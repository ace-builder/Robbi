"""Small local calculation tools; no eval, model or network required."""
import ast
import math
import operator

OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
             ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
FUNCTIONS = {name: getattr(math, name) for name in ("sqrt", "sin", "cos", "tan", "log", "log10", "floor", "ceil")}
FUNCTIONS["abs"] = abs
UNITS = {
    "mm": ("length", .001), "cm": ("length", .01), "m": ("length", 1), "km": ("length", 1000),
    "in": ("length", .0254), "ft": ("length", .3048), "yd": ("length", .9144), "mi": ("length", 1609.344),
    "mg": ("mass", .000001), "g": ("mass", .001), "kg": ("mass", 1), "oz": ("mass", .028349523125), "lb": ("mass", .45359237),
    "ml": ("volume", .001), "l": ("volume", 1), "us_gal": ("volume", 3.785411784),
    "m/s": ("speed", 1), "km/h": ("speed", 1/3.6), "mph": ("speed", .44704),
}


def checked(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e15:
        raise ValueError("Calculation is outside the supported finite range (±1e15)")
    return value


def calculate(expression):
    if not isinstance(expression, str) or not 1 <= len(expression) <= 200:
        raise ValueError("Expression must contain 1 to 200 characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise ValueError("Expression syntax is invalid; check the numbers, operators and parentheses") from None
    if len(list(ast.walk(tree))) > 64:
        raise ValueError("Expression is too complex (64 syntax nodes maximum)")
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return checked(node.value)
        if isinstance(node, ast.Name) and node.id in {"pi", "e"}:
            return getattr(math, node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return checked(visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS:
            if len(node.args) != 1 or node.keywords:
                raise ValueError("Math functions take exactly one numeric argument; angles are in radians")
            return checked(FUNCTIONS[node.func.id](visit(node.args[0])))
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("Exponent must be between -12 and 12")
            return checked(OPERATORS[type(node.op)](left, right))
        raise ValueError("Use numbers, pi, e, + - * / // % ** and sqrt, sin, cos, tan, log, log10, floor, ceil, abs only")
    return visit(tree)


def convert(value, source, target):
    if source in {"C", "F", "K"} and target in {"C", "F", "K"}:
        celsius = (value-32)*5/9 if source == "F" else value-273.15 if source == "K" else value
        if celsius < -273.15:
            raise ValueError("Temperature is below absolute zero")
        return celsius*9/5+32 if target == "F" else celsius+273.15 if target == "K" else celsius
    if source not in UNITS or target not in UNITS or UNITS[source][0] != UNITS[target][0]:
        raise ValueError("Choose two supported units of the same physical quantity")
    return value * UNITS[source][1] / UNITS[target][1]


def run(context, tool_name, arguments):
    context.checkpoint()
    try:
        if tool_name == "calculate":
            value = calculate(arguments["expression"])
            return {"ok": True, "text": f"{arguments['expression'].strip()} = {display(value)}", "data": {"value": value}}
        if tool_name != "convert":
            raise ValueError("Unknown calculator tool")
        value = checked(convert(arguments["value"], arguments["from_unit"], arguments["to_unit"]))
        return {"ok": True, "text": f"{display(arguments['value'])} {arguments['from_unit']} = {display(value)} {arguments['to_unit']}", "data": {"value": value, "unit": arguments["to_unit"]}}
    except ZeroDivisionError:
        return {"ok": False, "text": "Division by zero is undefined. Check the divisor or exponent."}
    except OverflowError:
        return {"ok": False, "text": "Calculation is outside the supported finite range (±1e15)"}
    except ValueError as error:
        text = str(error)
        if text == "math domain error":
            text = "That function is undefined for this input. Square roots need a nonnegative value; logarithms need a positive value."
        return {"ok": False, "text": text}


def display(value):
    # repr preserves a float's actual result instead of rounding to six digits
    # as the default :g formatter did. Whole numbers remain easy to read aloud.
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
