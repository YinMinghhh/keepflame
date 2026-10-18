"""Static, non-executing resolver for obfuscator.io-style string tables.

The ikuuu navigation page builds its domain list in an obfuscated inline script:
a rotated string table, a base64 decoder function, thin wrapper functions and
string concatenations. This module never executes JavaScript. It parses just
enough of that shape (numbers, string literals, + - * /, unary minus,
``parseInt`` and wrapper calls) to recover the concatenated string values, so a
hostile page can at most yield wrong strings, which the caller still probes.
"""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass

MAX_SCRIPT_CHARS = 1_000_000
MAX_TABLE = 20_000
MAX_EXPRESSION_CHARS = 4_000
MAX_DEPTH = 32

_ID = r"_0x[0-9a-fA-F]+"
_TABLE_FN = re.compile(r"function (" + _ID + r")\(\)\{const " + _ID + r"=\[((?:'[^'\\]*',?)*)\];")
_DECODER_FN = re.compile(
    r"function (" + _ID + r")\(" + _ID + r"," + _ID + r"\)\{const " + _ID + r"=(" + _ID + r")\(\);return \1=function\("
    + r"(" + _ID + r")," + _ID + r"\)\{\3=\3-\(([^;{}]{1,200})\);")
_WRAPPER_FN = re.compile(
    r"function (" + _ID + r")\(((?:" + _ID + r",?){1,6})\)\{(?:const " + _ID + r"=\{(?:" + _ID
    + r":-?0x[0-9a-fA-F]+,?)*\};)?return (" + _ID + r")\(([^;{}]{1,400})\);\}")
_OBJECT = re.compile(r"(?:const|let|var) (" + _ID + r")=\{((?:" + _ID + r":-?0x[0-9a-fA-F]+,?)+)\}")
_ROTATION = re.compile(
    r"\(function\((" + _ID + r")," + _ID + r"\)\{.{0,4000}?while\(!!\[\]\)\{try\{const " + _ID + r"=(.{1,3000}?);if\("
    + _ID + r"===" + _ID + r"\)break;.{0,200}?\}\}\}\((" + _ID + r"),([^)]{1,200})\)\);", re.S)
_CONCAT = re.compile(
    r"(?:'[^'\\\n]{0,200}'|" + _ID + r"\([^()\n]{0,200}\))(?:\+(?:'[^'\\\n]{0,200}'|" + _ID + r"\([^()\n]{0,200}\)))+")


class DecodeError(ValueError):
    """The script does not have the supported obfuscation shape."""


def _b64(text: str) -> str:
    # obfuscator.io's alphabet is base64 with letter case swapped.
    swapped = text.swapcase()
    swapped += "=" * (-len(swapped) % 4)
    try:
        return base64.b64decode(swapped, validate=False).decode("utf-8")
    except (ValueError, UnicodeError):
        return ""


_TOKEN = re.compile(r"\s*(0x[0-9a-fA-F]+|\d+(?:\.\d+)?|'[^'\\\n]*'|" + _ID + r"(?:\." + _ID + r")?|parseInt|[-+*/(),]|\S)")


@dataclass
class _Wrapper:
    params: tuple[str, ...]
    callee: str
    args: str


class _Resolver:
    def __init__(self, script: str) -> None:
        if len(script) > MAX_SCRIPT_CHARS:
            raise DecodeError("script too large")
        self.script = script
        table = _TABLE_FN.search(script)
        decoder = _DECODER_FN.search(script)
        if table is None or decoder is None:
            raise DecodeError("string table or decoder not found")
        self.table = re.findall(r"'([^'\\]*)'", table.group(2))
        if not 0 < len(self.table) <= MAX_TABLE:
            raise DecodeError("string table size unsupported")
        self.decoder = decoder.group(1)
        self.objects: dict[str, int] = {}
        for match in _OBJECT.finditer(script):
            for key, value in re.findall(r"(" + _ID + r"):(-?0x[0-9a-fA-F]+)", match.group(2)):
                self.objects[f"{match.group(1)}.{key}"] = int(value, 16)
        self.wrappers: dict[str, _Wrapper] = {}
        for match in _WRAPPER_FN.finditer(script):
            self.wrappers[match.group(1)] = _Wrapper(tuple(match.group(2).rstrip(",").split(",")),
                                                     match.group(3), match.group(4))
        self.offset = 0
        self.offset = int(self.evaluate(decoder.group(4), {}))
        self._rotate()

    # -- expression evaluation ----------------------------------------------------
    def evaluate(self, text: str, env: dict[str, object], depth: int = 0):
        if len(text) > MAX_EXPRESSION_CHARS or depth > MAX_DEPTH:
            raise DecodeError("expression too complex")
        tokens = [token for token in _TOKEN.findall(text) if token]
        position = 0

        def peek():
            return tokens[position] if position < len(tokens) else None

        def take():
            nonlocal position
            if position >= len(tokens):
                raise DecodeError("unexpected end")
            position += 1
            return tokens[position - 1]

        def primary():
            token = take()
            if token == "(":
                value = expression()
                if take() != ")":
                    raise DecodeError("unbalanced parenthesis")
                return value
            if token == "-":
                value = primary()
                if isinstance(value, str):
                    raise DecodeError("unary minus on string")
                return -value
            if token.startswith("'"):
                return token[1:-1]
            if token.startswith("0x"):
                return int(token, 16)
            if token[0].isdigit():
                return float(token) if "." in token else int(token)
            if peek() == "(":
                take()
                args = []
                if peek() != ")":
                    while True:
                        args.append(expression())
                        if peek() == ",":
                            take()
                            continue
                        break
                if take() != ")":
                    raise DecodeError("unbalanced call")
                return self.call(token, args, depth)
            if token in env:
                return env[token]
            if token in self.objects:
                return self.objects[token]
            raise DecodeError("unknown identifier")

        def term():
            value = primary()
            while peek() in ("*", "/"):
                operator = take()
                right = primary()
                if isinstance(value, str) or isinstance(right, str):
                    raise DecodeError("arithmetic on string")
                value = value * right if operator == "*" else (value / right if right else math.nan)
            return value

        def expression():
            value = term()
            while peek() in ("+", "-"):
                operator = take()
                right = term()
                if operator == "+":
                    value = (str(_js(value)) + str(_js(right))) if isinstance(value, str) or isinstance(right, str) else value + right
                else:
                    if isinstance(value, str) or isinstance(right, str):
                        raise DecodeError("subtraction on string")
                    value = value - right
            return value

        value = expression()
        if position != len(tokens):
            raise DecodeError("trailing tokens")
        return value

    def call(self, name: str, args: list, depth: int):
        if name == "parseInt":
            match = re.match(r"\s*([+-]?\d+)", str(args[0]) if args else "")
            return int(match.group(1)) if match else math.nan
        if name == self.decoder:
            if not args or isinstance(args[0], str) or args[0] != args[0]:
                raise DecodeError("bad decoder index")
            index = int(args[0]) - self.offset
            if not 0 <= index < len(self.table):
                raise DecodeError("decoder index out of range")
            return _b64(self.table[index])
        wrapper = self.wrappers.get(name)
        if wrapper is None:
            raise DecodeError("unknown function")
        env = dict(zip(wrapper.params, args + [0] * (len(wrapper.params) - len(args))))
        return self.evaluate(f"{wrapper.callee}({wrapper.args})", env, depth + 1)

    def _rotate(self) -> None:
        rotation = _ROTATION.search(self.script)
        if rotation is None:
            raise DecodeError("rotation loop not found")
        checksum, target = rotation.group(2), self.evaluate(rotation.group(4), {})
        for _ in range(len(self.table)):
            try:
                if self.evaluate(checksum, {}) == target:
                    return
            except DecodeError:
                pass
            self.table.append(self.table.pop(0))
        raise DecodeError("rotation checksum never matched")

    def strings(self) -> list[str]:
        found = []
        for match in _CONCAT.finditer(self.script):
            try:
                value = self.evaluate(match.group(0), {})
            except DecodeError:
                continue
            if isinstance(value, str):
                found.append(value)
        return found


def _js(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def decoded_strings(script: str) -> list[str]:
    """Return concatenated string values recovered from one obfuscated script."""
    return _Resolver(script).strings()
