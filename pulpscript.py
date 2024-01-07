# This file contains the logic for transpiling actual pulpscript code.
# It's more complicated than transpiling the assets, so it gets its own file
# to keep it self-contained.

import re

RELASSIGN = True # allow relative assignment in lua, e.g. `x += 1`

def is_id(varname):
    return re.match(r'^[a-zA-Z_]\w*$', varname) is not None

def sanitize_varname(varname):
    if is_id(varname):
        return varname
    varname = varname.replace('"', '\\"')
    return f"_G[\"{varname}\"]"

class PulpScriptContext:
    def __init__(self):
        self.indent = 1
        self.errors = []
        self.vars = set()
        self.var_usage = {}
        self.full_mimics = []
        
        # cache these at the start of each function
        self.funccache = []
        
        self.evobjs = []
        
        # for [PDXINFO] and [PTL] extension
        self.ext_pdxinfo = dict()
        self.ext_ptl = dict()
        
        # manual script target optimization
        self.script_tags = dict()
        
    def push_funccache(self):
        self.funccache.append(set())
        
    def add_script_tag(self, scriptsrc, name):
        san_name = "__OPTTAG__" + (scriptsrc + "_" + name).replace("-", "_").replace(" ", "_")
        if san_name in self.script_tags:
            return san_name
        
        self.script_tags[san_name] = {
            "scriptsrc": scriptsrc,
            "name": name
        }
        return san_name
    
    def pop_funccache(self):
        self.funccache = self.funccache[:-1]
    
    def get_funccache(self):
        return self.funccache[-1]
        
    def push_evobj(self, obj):
        self.evobjs.append(obj)
    
    def pop_evobj(self):
        self.evobjs = self.evobjs[:-1]
    
    def get_evobj(self):
        return self.evobjs[-1]
        
    def pingvar(self, varname):
        if "." in varname:
            # ignore these.
            return varname
            
        if varname.startswith("__PTLE_"):
            #ignore pulp-to-lua extension variables
            return varname
        
        self.var_usage[varname] = self.var_usage.get(varname, 0) + 1
        if varname.startswith("__"):
            # to prevent namespace conflicts, we append additional underscores if the variable name
            # starts with two underscores.
            varname = "__" + varname
        
        varname = sanitize_varname(varname)
        
        self.vars.add(varname)
        return varname
        
        
    # get-indent.
    def gi(self):
        return "  "*self.indent
        
    def commentconfigure(self, mode, conf):
        key, value = conf.split("=")
        key = key.strip()
        value = value.strip()
        if mode == "PTL":
            if value == "0" or value == "False" or value == "false":
                value = False
            self.ext_ptl[key] = value
        if mode == "PDXINFO":
            self.ext_pdxinfo[key] = value
        
compdict = {
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "eq": "==",
    "neq": "~=",
}

funclist = [
    "goto",
    "shake",
    "fin",
    "hide",
    "draw",
    "bpm",
    "sound",
    "invert",
    "frame",
    "fill",
    "swap",
    "label",
    "restore",
    "store",
    "toss",
    "wait",
    "say",
    "ask",
    "menu",
    "option",
    "act",
    "loop",
    "once",
    "stop",
    "tell",
    "ignore",
    "listen",
    "log",
    "dump",
    "wait",
    "window",
    "crop",
    "play",
]

# functions which can be used in expressions
exfuncs = [
    "type",
    "id",
    "solid",
    "frame",
    "floor",
    "round",
    "ceil",
    "invert",
    "name",
    "random",
    "sine",
    "cosine",
    "tangent",
    "degrees",
    "radians",
    "lpad", # args: (string, width, [padsymbol])
    "rpad", # args: (string, width, [padsymbol])
]

inlinefuncs = {
    "__fn_frame": "{0}.frame = {1}",
    "__fn_inc": "{0} += 1",
    "__fn_dec": "{0} -= 1",
    "__fn_log": "__print({0})",
    # OPTIMIZE: we can hardcode in __pix8scale to improve performance. __pix8scale is usually 1!
    # OPTIMIZE: we can probably perform the color decision at compile-time in most cases
    "__fn_fill": "__setcolour(__fillcolours[{4}]); __fillrect({0} * __pix8scale, {1} * __pix8scale, {2} * __pix8scale, {3} * __pix8scale)",
    "__ex_frame": "({0}.frame or 0)",
    "__ex_invert": "(__pulp.invert and 1 or 0)",
    "__ex_degrees": "({0} * 360 / __tau)",
    "__ex_radians": "({0} * __tau / 360)",
}

# specifies the order of the *first* arguments to these functions.
# additional arguments may follow!
funcargs = {
    "frame": ["actor"],
    "goto": ["x", "y"],
    "tell": ["event", "evname", "block"],
    "swap": ["actor"],
    "label": ["x", "y", "len", "lines"],
    "draw": ["x", "y"],
    "solid": ["x", "y"],
    "wait": ["actor", "event", "evname", "block"],
    "say": ["x", "y", "w", "h", "actor", "event", "evname", "block"],
    "ask": ["x", "y", "w", "h", "actor", "event", "evname", "block"],
    "menu": ["x", "y", "w", "h", "actor", "event", "evname", "block"],
    "option": ["actor", "event", "evname", "block"],
    "window": ["x", "y", "w", "h"],
    "fill": ["x", "y", "w", "h"],
    "crop": ["x", "y", "w", "h"],
    "play": ["actor", "event", "evname", "block"],
    "once": ["actor", "event", "evname", "block"],
    "type": ["x", "y"],
    "id": ["x", "y"],
}

staticfuncs = {
    "floor": "__floor",
    "ceil": "__ceil",
    "round": "__round",
    "sine": "__sin",
    "cosine": "__cos",
    "sine": "__sin",
    "tangent": "__tan",
    "random": "__random",
}

# adds backslashes
def escape_string(s):
    return s.replace("\\","\\\\") \
        .replace("\n", "\\n") \
        .replace("\f", "\\f") \
        .replace("\"", "\\\"")

# note that '.' is valid in a token, but not an id
def istoken(s):
    if type(s) != str:
        return False
    if " " in s:
        return False
    if "-" in s:
        return False
    if len(s) == 0:
        return False
    if s[0] in "0123456789":
        return False
    return True

tile_ids = dict() # populated in pulplua.py

def optimize_name_ref(cmd, idx):
    if len(cmd) > idx:
        if type(cmd[idx]) == str and cmd[idx] in tile_ids:
            cmd[idx] = [
                "optimized-id",
                tile_ids[cmd[idx]],
                cmd[idx]
            ]
            return True
    return False
    
def remap_special_varname(varname, ctx):
    # these require special caching behaviour per-function
    # note that they cannot be set (op_set), so we don't need to consider them there.
    if varname == "event.px":
        ctx.get_funccache().add("local __event_px = __pulp.player.x")
        return "__event_px"
    elif varname == "event.py":
        ctx.get_funccache().add("local __event_py = __pulp.player.y")
        return "__event_py"
    elif varname == "event.x":
        ctx.get_funccache().add("local __event_x = __actor.x or __pulp.player.x")
        return "__event_x"
    elif varname == "event.y":
        ctx.get_funccache().add("local __event_y = __actor.y or __pulp.player.y")
        return "__event_y"
    elif varname == "event.dx":
        ctx.get_funccache().add("local __event_dx = event.dx or 0")
        return "__event_dx"
    elif varname == "event.dy":
        ctx.get_funccache().add("local __event_dy = event.dy or 0")
        return "__event_dy"
    elif varname == "event.tile":
        ctx.get_funccache().add("local __event_tile = __actor.name or 0")
        return "__event_tile"
    elif varname == "event.room":
        return "event.room.name"
    elif varname == "event.player":
        return "__pulp.player.name"
    elif varname == "datetime.year":
        return "__getTime().year"
    elif varname == "datetime.year99":
        return "--[[(year99)]] (__getTime().year % 100)"
    elif varname == "datetime.month":
        return "__getTime().month"
    elif varname == "datetime.day":
        return "__getTime().day"
    elif varname == "datetime.weekday":
        return "(__getTime().weekday - 1)"
    elif varname == "datetime.day":
        return "__getTime().day"
    elif varname == "datetime.hour":
        return "__getTime().hour"
    elif varname == "datetime.hour12":
        return "--[[(hour12)]] ((__getTime().hour % 12) + 1)"
    elif varname == "datetime.minute":
        return "__getTime().minute"
    elif varname == "datetime.second":
        return "__getTime().second"
    elif varname == "datetime.millisecond": #note: pulp-to-lua extension
        return "__getTime().millisecond --[[(PTL-only?)]]"
    elif varname == "datetime.ampm":
        return "--[[(ampm)]] (__getTime().hour < 12 and \"am\" or \"pm\")"
    elif varname == "datetime.AMPM": #note: pulp-to-lua extension
        return "--[[(AMPM)]] (__getTime().hour < 12 and \"AM\" or \"PM\")  --[[(PTL-only?)]]"
    elif varname == "datetime.timestamp":
        return "__getSecondsSinceEpoch()"
    elif varname == "__PTLE_SMOOTH_MOVEMENT_SPEED":
        return "__pulp.PTLE_SMOOTH_MOVEMENT_SPEED"
    elif varname == "__PTLE_SMOOTH_OFFSET_X":
        return "__pulp.PTLE_SMOOTH_OFFSET_X"
    elif varname == "__PTLE_SMOOTH_OFFSET_Y":
        return "__pulp.PTLE_SMOOTH_OFFSET_Y"
    elif varname == "__PTLE_CONFIRM_DAS":
        return "__pulp.PTLE_CONFIRM_DAS"
    elif varname == "__PTLE_CANCEL_DAS":
        return "__pulp.PTLE_CANCEL_DAS"
    elif varname == "__PTLE_V_DAS":
        return "__pulp.PTLE_V_DAS"
    elif varname == "__PTLE_H_DAS":
        return "__pulp.PTLE_H_DAS"
    
    return varname

def ex_get(expression, ctx):
    return remap_special_varname(ctx.pingvar(expression[1]), ctx)
    
    
def ex_format(expression, ctx):
    s = ""
    first = True
    for component in expression[1:]:
        if not first:
            s += " .. "
        first = False
        if type(component) is str:
            s += decode_rvalue(component, ctx)
        else:
            s += "__tostring(" + decode_rvalue(component, ctx) + ")"
            
    return s

def ex_embed(expression, ctx):
    return f"__pulp.__ex_embed({decode_rvalue(expression[1], ctx)})"

def ex_subroutine(expression, ctx):
    s = "function(__actor, event, evname)\n"
    ctx.indent += 2
    s += transpile_commands(ctx.blocks[expression[1]], ctx, True)
    ctx.indent -= 2
    return s + ctx.gi() + "  end"
    
def ex_name(expression, ctx):
    if type(expression[1]) == list and expression[1][0] == "xy":
        x = decode_rvalue(expression[1][1], ctx)
        y = decode_rvalue(expression[1][2], ctx)
        #return f"(((__roomtiles[{y}] or __pulp.EMPTY)[{x}] or __pulp.EMPTY).tile or __pulp.EMPTY).name or \"\""
        return f"__roomtiles[{y}][{x}].name"
    else:
        return opex_func(expression, "name", "__ex_", ctx)

def decode_rvalue(expression, ctx):
    if type(expression) == str:
        return '"' + escape_string(str(expression)) + '"'
    elif type(expression) == int or type(expression) == float:
        return str(expression)
    elif expression is None:
        return "nil"
    else:
        ex = expression[0]
        if ex == "get":
            return ex_get(expression, ctx)
        elif ex == "optimized-id":
            return f"--[[({expression[2]})]] "+str(expression[1])
        elif ex == "format":
            return ex_format(expression, ctx)
        elif ex == "embed":
            return ex_embed(expression, ctx)
        elif ex == "name":
            return ex_name(expression, ctx)
        elif ex in exfuncs:
            return opex_func(expression, ex, "__ex_", ctx)
        elif ex == "block":
            return ex_subroutine(expression, ctx)
        else:
            ctx.errors += ["unknown expression code: " + ex]
            return f"nil --[[unknown expression code '{ex}']]"

def op_set(cmd, operator, ctx):
    lvalue = cmd[1]
    assert (type(lvalue) == str)
    lvalue = ctx.pingvar(lvalue)
    rvalue = decode_rvalue(cmd[2], ctx)
    if operator == "" or RELASSIGN:
        return f"{lvalue} {operator}= {rvalue}"
    else:
        return f"{lvalue} = {lvalue} {operator} {rvalue}"

def op_block(cmd, statement, follow, end, ctx):
    condition = cmd[1]
    comparison = condition[0]
    assert comparison in compdict, f"unrecognized comparison operator '{comparison}'"
    compsym = compdict[comparison]
    if istoken(condition[1]):
        compl = condition[1]
        compl = remap_special_varname(ctx.pingvar(compl), ctx)
    else:
        compl = decode_rvalue(condition[1], ctx)
    compr = decode_rvalue(condition[2], ctx)
    block = cmd[2]
    assert(block[0] == "block")
    s = f"{statement} {compl} {compsym} {compr} {follow}\n"
    ctx.indent += 1
    s += transpile_commands(ctx.blocks[block[1]], ctx)
    ctx.indent -= 1
    
    for sub in cmd[3:]:
        if sub[0] == "elseif":
            s += ctx.gi() + op_block(sub, "elseif", "then", None, ctx)
        elif sub[0] == "else":
            s += ctx.gi() + "else\n"
            ctx.indent += 1
            block = sub[1]
            assert(block[0] == "block")
            s += transpile_commands(ctx.blocks[block[1]], ctx)
            ctx.indent -= 1
            pass
        else:
            assert False, f"unrecognized block followup '{sub[0]}'"
    if end:
        s += ctx.gi() + end
    return s

def op_call(cmd, ctx, tags):
    global EVNAMECOUNTER
    
    # NOTE: it's important that we use '__self' here, as *mimic* calls do not call back virtually to the original
    # actor's script.
    # ctx.get_funccache().add(f"local __evobj = {ctx.get_evobj()}")
    
    if istoken(cmd[1]):
        fnstr = f"\"{cmd[1]}\""
        callfn = f"{ctx.get_evobj()}.{cmd[1]}"
    else:
        fnstr = decode_rvalue(cmd[1], ctx)
        callfn = f"{ctx.get_evobj()}[{fnstr}]"
        
    fnbase = f";({callfn} or {ctx.get_evobj()}.any)"
    
    comment = f"--[call \"{fnstr[1:-1]}\"]"
    
    if "[DIRECT]" in tags:
    
        fnbase = callfn
        comment += " [DIRECT]"
            
    
    if ctx.get_evobj() == "__self":
        ctx.get_funccache().add(f"local __self = {ctx.root_evobj} --[this script]")
        
        # simplification if we know the name of the function
        if istoken(cmd[1]):
            if cmd[1] in list(ctx.self_evnames):
                fnbase = callfn
                comment = ""
            else:
                fnbase = "__self.any"
                comment += " [doesn't exist, so any]"
                
    for tag in tags:
        if tag.startswith("[SCRIPT:"):
            scriptsrc = tag[len("[SCRIPT:"):-1]
            fnbase = ctx.add_script_tag(scriptsrc, fnstr[1:-1])
            comment += " " + tag
    
    return f"{fnbase}(__actor, event, {fnstr}) {comment}"
        
def op_emit(cmd, ctx):
    return f"__pulp:emit({decode_rvalue(cmd[1], ctx)}, event)"
    
def op_mimic(cmd, ctx):
    if optimize_name_ref(cmd, 1) or type(cmd[1]) == int:
        s = f"do -- (mimic)\n"
        ctx.indent += 1
        s += ctx.gi() + f"local __mimic_target__ = (__pulp.tiles[{decode_rvalue(cmd[1], ctx)}] or __pulp.EMPTY).script;\n"
        s += ctx.gi() + "(__mimic_target__[__evname] or __mimic_target__.any)(__actor, event, __evname)\n"
        ctx.indent -= 1
        s += ctx.gi() + "end"
        return s
    else:
        s = "do -- (mimic)\n"
        ctx.indent += 1
        s += ctx.gi() + f"local __mimic_target__ = __pulp:getScript({decode_rvalue(cmd[1], ctx)}) or __pulp.EMPTY;\n"
        s += ctx.gi() + f"(__mimic_target__[__evname] or __mimic_target__.any)(__actor, event, __evname)\n"
        ctx.indent -= 1
        s += ctx.gi() + "end"
        return s
    
def op_tell(cmd, ctx):
    if type(cmd[1]) == list and cmd[1][0] == "xy":
        # inline version of 'tell x,y to'
        s = "do --[tell x,y to]\n"
        ctx.indent += 1
        assert cmd[2][0] == "block"
        s += ctx.gi() + f"local __actor = __roomtiles[{decode_rvalue(cmd[1][2], ctx)}][{decode_rvalue(cmd[1][1], ctx)}]\n"
        s += ctx.gi() + f"if __actor and __actor.tile then\n"
        ctx.indent += 1
        ctx.push_evobj("__actor.script")
        assert ctx.get_evobj() != "__self"
        s += transpile_commands(ctx.blocks[cmd[2][1]], ctx, True)
        ctx.pop_evobj()
        ctx.indent -= 1
        s += ctx.gi() + f"end\n"
        ctx.indent -= 1
        s += ctx.gi() + "end\n"
        return s
    elif type(cmd[1]) == list and cmd[1][0] == "get" and cmd[1][1] in ["event.room", "event.game", "event.player"]:
        # inline version of 'tell event.X to'
        target = cmd[1][1]
        if target == "event.player":
            target = "__pulp.player"
        s = f"do --[tell {target} to]\n"
        ctx.indent += 1
        assert cmd[2][0] == "block"
        s += ctx.gi() + f"local __actor = {target}\n"
        s += ctx.gi() + f"if __actor then\n"
        ctx.indent += 1
        ctx.push_evobj("__actor.script")
        s += transpile_commands(ctx.blocks[cmd[2][1]], ctx, True)
        ctx.pop_evobj()
        ctx.indent -= 1
        s += ctx.gi() + f"end\n"
        ctx.indent -= 1
        s += ctx.gi() + "end\n"
        return s
    else:
        optimize_name_ref(cmd, 1)
        ctx.push_evobj("__actor.script")
        s = opex_func(cmd, "tell", "__fn_", ctx)
        ctx.pop_evobj()
        return s
    
def opex_func(cmd, op, prefix, ctx):
    mainargs = []
    setargs = {
        "actor": "__actor",
        "event": "event",
        "evname": "__evname",
    }
        
    for arg in cmd[1:]:
        if type(arg) == list:
            if arg[0] == "xy":
                setargs["x"] = decode_rvalue(arg[1], ctx)
                setargs["y"] = decode_rvalue(arg[2], ctx)
            elif arg[0] == "rect":
                setargs["x"] = decode_rvalue(arg[1], ctx)
                setargs["y"] = decode_rvalue(arg[2], ctx)
                setargs["w"] = decode_rvalue(arg[3], ctx)
                if len(arg) > 4: # pulp game 'Monitor Duty' needs this guard
                    setargs["h"] = decode_rvalue(arg[4], ctx)
            elif arg[0] == "block":
                # e.g. "then" or "to"
                setargs["block"] = decode_rvalue(arg, ctx)
            else:
                mainargs.append(decode_rvalue(arg, ctx))
        else:
            mainargs.append(decode_rvalue(arg, ctx))
    
    if op in funcargs:
        for argname in funcargs[op][::-1]:
            if argname in setargs:
                mainargs = [setargs[argname]] + mainargs
            else:
                mainargs = ["nil"] + mainargs
    
    if prefix + op in inlinefuncs:
        s = inlinefuncs[prefix + op]
        for i in range(len(mainargs)):
            s = s.replace("{" + str(i) + "}", mainargs[i])
        return s
    elif op in staticfuncs:
        s = staticfuncs[op] + "("
    else:
        s = f"__pulp.{prefix}{op}("
    first = True
    for arg in mainargs:
        if first:
            first = False
        else:
            s += ", "
        s += arg
    return s + ")"

def op_inc(cmd, operator, ctx):
    return cmd[1] + operator
    
def op_random(cmd, ctx):
    if len(cmd) == 2:
        return f"__random({decode_rvalue(cmd[1], ctx)})"
    elif len(cmd) == 3:
        return f"__random({decode_rvalue(cmd[1], ctx)}, {decode_rvalue(cmd[2], ctx)})"
    else:
        assert False, "wrong number of arguments for 'random'"
    
def comment(cmd, prevline, ctx):
    s = "--"
    idx = cmd[1]
    comment = "<comment missing>"
    if idx < len(ctx.comments_block):
        comment = ctx.comments_block[idx]
    if "\n" in comment:
        s += "[["
    
    # PTL extension: raw LUA code.
    stripcomment = comment.strip()
    if stripcomment.startswith("[LUA]"):
        s = comment.strip()[len("[LUA]"):];
        if s.startswith(" "):
          s = s[1:]
        return s
        
    # PTL extension: PDXINFO/PTL
    
    if stripcomment.startswith("[PDXINFO]"):
        ctx.commentconfigure("PDXINFO", stripcomment[len("[PDXINFO]"):].strip())
        
    if stripcomment.startswith("[PTL]"):
        ctx.commentconfigure("PTL", stripcomment[len("[PTL]"):].strip())
    
    if prevline:
        s += "^"
    
    s += comment
        
    if "\n" in comment:
        s += "]]"
        
    return s
        
def get_next_cmds_tags(ctx, cmds):
    tags = []
    for cmd in cmds:
        if type(cmd) == list:
            if cmd[0] in ["#", "#$"]:
                idx = cmd[1]
                if idx < len(ctx.comments_block):
                    comment = ctx.comments_block[idx].strip()
                    if comment.startswith("["):
                        tags.append(comment)
        else:
            break
    return tags

def transpile_command(cmd, ctx, next_cmds):
    op = cmd[0]
    
    tags = get_next_cmds_tags(ctx, next_cmds)
    
    if op == "_":
        return "" # ctx.gi() + "\n"
    elif op == "done":
        return ctx.gi() + "do return end\n"
    elif op == "set":
        return ctx.gi() + op_set(cmd, "", ctx) + "\n"
    elif op == "add":
        return ctx.gi() + op_set(cmd, "+", ctx) + "\n"
    elif op == "sub":
        return ctx.gi() + op_set(cmd, "-", ctx) + "\n"
    elif op == "div":
        return ctx.gi() + op_set(cmd, "/", ctx) + "\n"
    elif op == "mul":
        return ctx.gi() + op_set(cmd, "*", ctx) + "\n"
    elif op == "inc":
        return ctx.gi() + op_inc(cmd, "+=1", ctx) + "\n"
    elif op == "dec":
        return ctx.gi() + op_inc(cmd, "-=1", ctx) + "\n"
    elif op == "random":
        return ctx.gi() + op_random(cmd, ctx) + "\n"
    elif op == "if":
        return ctx.gi() + op_block(cmd, "if", "then", "end", ctx) + "\n"
    elif op == "while":
        return ctx.gi() + op_block(cmd, "while", "do", "end", ctx) + "\n"
    elif op == "call":
        return ctx.gi() + op_call(cmd, ctx, tags) + "\n"
    elif op == "emit":
        return ctx.gi() + op_emit(cmd, ctx) + "\n"
    elif op == "mimic":
        return ctx.gi() + op_mimic(cmd, ctx) + "\n"
    elif op == "tell":
        return ctx.gi() + op_tell(cmd, ctx) + "\n"
    elif op in funclist:
        return ctx.gi() + opex_func(cmd, op, "__fn_", ctx) + "\n"
    elif op in ["#", "#$"]:
        return ctx.gi() + comment(cmd, op == "#$", ctx) + "\n"
    else:
        ctx.errors += ["unknown command code: " + op]
        return ctx.gi() + f"--unknown command code '{op}'\n"

def transpile_commands(commands, ctx, has_funccache=False):
    if has_funccache:
        ctx.push_funccache()
    s = ""
    for i in range(len(commands)):
        command = commands[i]
        if type(command) == list:
            s += transpile_command(command, ctx, commands[i+1:])
    if has_funccache:
        for cached in sorted(list(ctx.get_funccache())):
            s = ctx.gi() + cached + "\n" + s
        ctx.pop_funccache()
    return s
        
def transpile_event(evobj, evname, ctx, blockidx, evobjname, comments_block, evnames):
    _evobj = f"__pulp:getScript(\"{evobj}\")" # evobjname would be faster, but less clear.
    if istoken(evname):
        s = f"{_evobj}.{evname} = function(__actor, event, __evname)\n"
    else:
        s = f"{_evobj}[\"{evname}\"] = function(__actor, event, __evname)\n"
    
    block = ctx.blocks[blockidx]
    
    ctx.self_evnames = evnames
    ctx.comments_block = comments_block
    ctx.push_evobj("__self")
    ctx.root_evobj = evobjname if evobjname else _evobj
    cmdstr = transpile_commands(ctx.blocks[blockidx], ctx, True)
    ctx.pop_evobj()
    
    s += cmdstr
    s += "end\n"
    
    #optimization for one-line-only mimics
    undecorated_block = list(filter(lambda x: type(x) == list and x[0] not in ["_", "#", "#$"], block))
    if len(undecorated_block) == 1 and undecorated_block[0][0] == "mimic":
        mimic = undecorated_block[0]
        # TODO: if int instead of optimized-id
        if type(mimic[1]) == list and mimic[1][0] == "optimized-id":
            mimic[1][2]
            ctx.full_mimics.append((evobj, evname, mimic[1][2]))
    return s