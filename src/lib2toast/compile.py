"""Compile a CST to an AST."""

import ast
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    Generator,
    Generic,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

from blib2to3 import pygram
from blib2to3.pgen2 import token
from blib2to3.pgen2.grammar import Grammar
from blib2to3.pytree import NL, Leaf, Node, type_repr
from typing_extensions import TypedDict

pygram.initialize(cache_dir=None)

syms = pygram.python_symbols

T = TypeVar("T")


def parse(code: str, grammar: Grammar = pygram.python_grammar_soft_keywords) -> NL:
    """Parse the given code using the given grammar."""
    driver = pygram.driver.Driver(grammar)
    return driver.parse_string(code, debug=True)


class UnsupportedSyntaxError(Exception):
    """Raised when some syntax is not supported on the current Python version."""


@dataclass
class Visitor(Generic[T]):
    token_type_to_name: Dict[int, str] = field(default_factory=lambda: token.tok_name)
    node_type_to_name: Callable[[int], str] = field(default_factory=lambda: type_repr)

    def get_node_name(self, node: NL) -> str:
        if node.type < 256:
            return self.token_type_to_name[node.type]
        else:
            return self.node_type_to_name(node.type)

    def visit(self, node: NL) -> T:
        name = self.get_node_name(node)
        method = getattr(self, f"visit_{name}", self.generic_visit)
        return method(node)

    def generic_visit(self, node: Node) -> T:
        raise NotImplementedError(f"visit_{self.get_node_name(node)}")


class LineRange(TypedDict):
    lineno: int
    col_offset: int
    end_lineno: int
    end_col_offset: int


def get_line_range_for_leaf(leaf: Leaf) -> LineRange:
    num_newlines = leaf.value.count("\n")
    if num_newlines == 0:
        end_col_offset = leaf.column + len(leaf.value)
    else:
        end_col_offset = len(leaf.value) - leaf.value.rfind("\n") - 1
    return LineRange(
        lineno=leaf.lineno,
        col_offset=leaf.column,
        end_lineno=leaf.lineno + num_newlines,
        end_col_offset=end_col_offset,
    )


def get_line_range(node: NL) -> LineRange:
    if isinstance(node, Leaf):
        return get_line_range_for_leaf(node)
    else:
        begin_range = get_line_range(node.children[0])
        end_range = get_line_range(node.children[-1])
        return unify_line_ranges(begin_range, end_range)


def unify_line_ranges(begin_range: LineRange, end_range: LineRange) -> LineRange:
    return LineRange(
        lineno=begin_range["lineno"],
        col_offset=begin_range["col_offset"],
        end_lineno=end_range["end_lineno"],
        end_col_offset=end_range["end_col_offset"],
    )


TOKEN_TYPE_TO_BINOP = {
    token.PLUS: ast.Add,
    token.MINUS: ast.Sub,
    token.STAR: ast.Mult,
    token.SLASH: ast.Div,
    token.AT: ast.MatMult,
    token.DOUBLESLASH: ast.FloorDiv,
    token.PERCENT: ast.Mod,
    token.DOUBLESTAR: ast.Pow,
    token.LEFTSHIFT: ast.LShift,
    token.RIGHTSHIFT: ast.RShift,
    token.AMPER: ast.BitAnd,
    token.VBAR: ast.BitOr,
    token.CIRCUMFLEX: ast.BitXor,
}
TOKEN_TYPE_TO_COMPARE_OP = {
    token.EQEQUAL: ast.Eq,
    token.NOTEQUAL: ast.NotEq,
    token.LESS: ast.Lt,
    token.GREATER: ast.Gt,
    token.LESSEQUAL: ast.LtE,
    token.GREATEREQUAL: ast.GtE,
}
NAME_TO_COMPARE_OP = {"is": ast.Is, "in": ast.In}
TOKEN_TYPE_TO_UNARY_OP = {
    token.PLUS: ast.UAdd,
    token.MINUS: ast.USub,
    token.TILDE: ast.Invert,
}


@dataclass
class _Consumer:
    children: Sequence[NL]
    index: int = 0

    def consume(self, typ: Optional[int] = None) -> Optional[NL]:
        if self.index < len(self.children) and (
            typ is None or self.children[self.index].type == typ
        ):
            node = self.children[self.index]
            self.index += 1
            return node
        else:
            return None

    def done(self) -> bool:
        return self.index >= len(self.children)


class Compiler(Visitor[ast.AST]):
    expr_context: ast.expr_context = ast.Load()

    @contextmanager
    def set_expr_context(
        self, expr_context: ast.expr_context
    ) -> Generator[None, None, None]:
        old_expr_context = self.expr_context
        self.expr_context = expr_context
        try:
            yield
        finally:
            self.expr_context = old_expr_context

    def visit_typevar(self, node: Node) -> ast.AST:
        if sys.version_info >= (3, 12):
            bound = None
            default = None
            for index in (1, 3):
                if len(node.children) > index:
                    punc = node.children[index]
                    if punc.type == token.COLON:
                        bound = self.visit(node.children[index + 1])
                    elif punc.type == token.EQUAL:
                        default = self.visit(node.children[index + 1])
            if sys.version_info >= (3, 13):
                return ast.TypeVar(
                    name=node.children[0].value,
                    bound=bound,
                    default_value=default,
                    **get_line_range(node),
                )
            else:
                if default is not None and sys.version_info < (3, 13):
                    raise UnsupportedSyntaxError("TypeVar default")
                return ast.TypeVar(
                    name=node.children[0].value, bound=bound, **get_line_range(node)
                )
        else:
            raise UnsupportedSyntaxError("TypeVar")

    def visit_paramspec(self, node: Node) -> ast.AST:
        if sys.version_info >= (3, 13):
            if len(node.children) == 4:
                default_value = self.visit(node.children[3])
            else:
                default_value = None
            return ast.ParamSpec(
                name=node.children[0].value,
                default_value=default_value,
                **get_line_range(node),
            )
        if sys.version_info >= (3, 12):
            return ast.ParamSpec(name=node.children[0].value, **get_line_range(node))
        else:
            raise UnsupportedSyntaxError("ParamSpec")

    def visit_typevartuple(self, node: Node) -> ast.AST:
        if sys.version_info >= (3, 13):
            if len(node.children) == 4:
                default_value = self.visit(node.children[3])
            else:
                default_value = None
            return ast.TypeVarTuple(
                name=node.children[0].value,
                default_value=default_value,
                **get_line_range(node),
            )
        if sys.version_info >= (3, 12):
            return ast.TypeVarTuple(name=node.children[0].value, **get_line_range(node))
        else:
            raise UnsupportedSyntaxError("TypeVarTuple")

    def visit_typeparam(self, node: Node) -> ast.AST:
        return self.visit(node.children[0])

    def visit_file_input(self, node: Node) -> ast.AST:
        return ast.Module(
            # skip ENDMARKER
            body=[self.visit(child) for child in node.children[:-1]],
            type_ignores=[],  # TODO
            **get_line_range(node),
        )

    # Statements
    def visit_simple_stmt(self, node: Node) -> ast.AST:
        val = self.visit(node.children[0])
        if isinstance(val, ast.expr):
            return ast.Expr(val, **get_line_range(node.children[0]))
        else:
            return val

    # Expressions
    def visit_testlist_gexp(
        self, node: Node, parent_node: Optional[Node] = None
    ) -> ast.AST:
        if parent_node is None:
            parent_node = node
        if node.children[1].type == syms.old_comp_for:
            elt = self.visit(node.children[0])
            comps = self._compile_comprehension(node.children[1])
            return ast.GeneratorExp(
                elt=elt, generators=comps, **get_line_range(parent_node)
            )
        elts = [self.visit(child) for child in node.children[::2]]
        return ast.Tuple(
            elts=elts, ctx=self.expr_context, **get_line_range(parent_node)
        )

    def visit_atom(self, node: Node) -> ast.AST:
        if node.children[0].type == token.LPAR:
            if len(node.children) == 2:
                return ast.Tuple(elts=[], ctx=self.expr_context, **get_line_range(node))
            # tuples, parenthesized expressions
            if node.children[1].type == syms.testlist_gexp:
                return self.visit_testlist_gexp(node.children[1], node)
            return self.visit(node.children[1])
        elif node.children[0].type == token.LSQB:
            if len(node.children) == 2:
                return ast.List(elts=[], ctx=self.expr_context, **get_line_range(node))
            # lists
            inner = node.children[1]
            if inner.type != syms.listmaker:
                return ast.List(
                    elts=[self.visit(inner)],
                    ctx=self.expr_context,
                    **get_line_range(node),
                )
            if inner.children[1].type == syms.old_comp_for:
                elt = self.visit(inner.children[0])
                comps = self._compile_comprehension(inner.children[1])
                return ast.ListComp(elt=elt, generators=comps, **get_line_range(node))
            elts = [self.visit(child) for child in inner.children[::2]]
            return ast.List(elts=elts, ctx=self.expr_context, **get_line_range(node))
        elif node.children[0].type == token.LBRACE:
            if len(node.children) == 2:
                return ast.Dict(keys=[], values=[], **get_line_range(node))
            # sets, dicts
            inner = node.children[1]
            if inner.type != syms.dictsetmaker:
                return ast.Set(
                    elts=[self.visit(inner)],
                    ctx=self.expr_context,
                    **get_line_range(node),
                )
            consumer = _Consumer(inner.children)
            is_dict = False
            keys = []
            values = []
            elts = []
            while not consumer.done():
                if consumer.consume(token.DOUBLESTAR) is not None:
                    is_dict = True
                    expr = consumer.consume()
                    keys.append(None)
                    values.append(self.visit(expr))
                elif star_expr := consumer.consume(syms.star_expr):
                    elts.append(
                        ast.Starred(
                            value=self.visit(consumer.consume()),
                            ctx=self.expr_context,
                            **get_line_range(star_expr),
                        )
                    )
                else:
                    key_node = consumer.consume()
                    if (walrus := consumer.consume(token.COLONEQUAL)) is not None:
                        value_node = consumer.consume()
                        elt = self._compile_named_expr((key_node, walrus, value_node))
                        elts.append(elt)
                    elif consumer.consume(token.COLON) is not None:
                        key = self.visit(key_node)
                        value = self.visit(consumer.consume())
                        keys.append(key)
                        values.append(value)
                        is_dict = True
                    else:
                        elts.append(self.visit(key_node))
                    if comp_for := consumer.consume(syms.comp_for):
                        comps = self._compile_comprehension(comp_for)
                        if is_dict:
                            assert len(keys) == 1, keys
                            assert len(values) == 1, values
                            assert not elts, elts
                            return ast.DictComp(
                                key=keys[0],
                                value=values[0],
                                generators=comps,
                                **get_line_range(node),
                            )
                        else:
                            assert len(elts) == 1, elts
                            assert not keys, keys
                            assert not values, values
                            return ast.SetComp(
                                elt=elts[0], generators=comps, **get_line_range(node)
                            )
                if not consumer.done():
                    comma = consumer.consume(token.COMMA)
                    assert comma is not None
            if is_dict:
                assert not elts, elts
                return ast.Dict(keys=keys, values=values, **get_line_range(node))
            else:
                assert not keys, keys
                assert not values, values
                return ast.Set(elts=elts, ctx=self.expr_context, **get_line_range(node))
        elif node.children[0].type == token.DOT:
            # ellipsis
            assert len(node.children) == 3
            assert all(child.type == token.DOT for child in node.children)
            return ast.Constant(value=Ellipsis, **get_line_range(node))
        elif node.children[0].type == token.BACKQUOTE:
            # repr. Why not support it?
            callee = ast.Name(id="repr", ctx=ast.Load(), **get_line_range(node))
            return ast.Call(
                func=callee,
                args=[self.visit(node.children[1])],
                keywords=[],
                **get_line_range(node),
            )
        else:
            # concatenated strings, I think
            raise NotImplementedError(repr(node))

    def visit_expr(self, node: Node) -> ast.AST:
        op = self.visit(node.children[0])
        begin_range = get_line_range(node.children[0])
        for child_index in range(2, len(node.children), 2):
            child = node.children[child_index]
            operator = node.children[child_index - 1]
            op = ast.BinOp(
                left=op,
                op=TOKEN_TYPE_TO_BINOP[operator.type](),
                right=self.visit(child),
                **unify_line_ranges(begin_range, get_line_range(child)),
            )
        return op

    visit_xor_expr = visit_and_expr = visit_shift_expr = visit_arith_expr = (
        visit_term
    ) = visit_expr

    def visit_comparison(self, node: Node) -> ast.AST:
        left = self.visit(node.children[0])
        ops: list[ast.cmpop] = []
        comparators: list[ast.expr] = []
        for child_index in range(2, len(node.children), 2):
            child = node.children[child_index]
            operator_node = node.children[child_index - 1]
            if operator_node.type == token.NAME:
                operator = NAME_TO_COMPARE_OP[operator_node.value]()
            elif isinstance(operator_node, Leaf):
                operator = TOKEN_TYPE_TO_COMPARE_OP[operator_node.type]()
            else:
                # is not, not in
                assert len(operator_node.children) == 2
                if operator_node.children[0].value == "not":
                    assert operator_node.children[1].value == "in"
                    operator = ast.NotIn()
                else:
                    assert operator_node.children[0].value == "is"
                    assert operator_node.children[1].value == "not"
                    operator = ast.IsNot()
            ops.append(operator)
            right = self.visit(child)
            assert isinstance(right, ast.expr)
            comparators.append(right)
        return ast.Compare(
            left=left, ops=ops, comparators=comparators, **get_line_range(node)
        )

    def visit_star_expr(self, node: Node) -> ast.AST:
        return ast.Starred(
            value=self.visit(node.children[1]),
            ctx=self.expr_context,
            **get_line_range(node),
        )

    def visit_not_test(self, node: Node) -> ast.AST:
        return ast.UnaryOp(
            op=ast.Not(), operand=self.visit(node.children[1]), **get_line_range(node)
        )

    def visit_and_test(self, node: Node) -> ast.AST:
        operands = []
        for child in node.children[::2]:
            operand = self.visit(child)
            assert isinstance(operand, ast.expr)
            operands.append(operand)
        return ast.BoolOp(op=ast.And(), values=operands, **get_line_range(node))

    def visit_or_test(self, node: Node) -> ast.AST:
        operands = []
        for child in node.children[::2]:
            operand = self.visit(child)
            assert isinstance(operand, ast.expr)
            operands.append(operand)
        return ast.BoolOp(op=ast.Or(), values=operands, **get_line_range(node))

    def visit_test(self, node: Node) -> ast.AST:
        # must be if-else
        assert len(node.children) == 5
        assert node.children[1].value == "if"
        assert node.children[3].value == "else"
        return ast.IfExp(
            test=self.visit(node.children[2]),
            body=self.visit(node.children[0]),
            orelse=self.visit(node.children[4]),
            **get_line_range(node),
        )

    def visit_factor(self, node: Node) -> ast.AST:
        return ast.UnaryOp(
            op=TOKEN_TYPE_TO_UNARY_OP[node.children[0].type](),
            operand=self.visit(node.children[1]),
            **get_line_range(node),
        )

    def visit_power(self, node: Node) -> ast.AST:
        children = node.children
        if len(children) > 2 and node.children[-2].type == token.DOUBLESTAR:
            operand = self.visit(children[-1])
            return ast.BinOp(
                left=self._visit_power_without_power(children[:-2]),
                op=ast.Pow(),
                right=operand,
                **get_line_range(node),
            )
        else:
            return self._visit_power_without_power(children)

    def _visit_power_without_power(self, children: Sequence[NL]) -> ast.AST:
        if children[0].type == token.AWAIT:
            line_range = unify_line_ranges(
                get_line_range(children[0]), get_line_range(children[-1])
            )
            return ast.Await(
                value=self._visit_power_without_await(children[1:]), **line_range
            )
        else:
            return self._visit_power_without_await(children)

    def _visit_power_without_await(self, children: Sequence[NL]) -> ast.AST:
        atom = self.visit(children[0])
        begin_range = get_line_range(children[0])
        for trailer in children[1:]:
            if trailer.children[0].type == token.LPAR:  # call
                if len(trailer.children) == 2:
                    args: list[ast.expr] = []
                    keywords: list[ast.keyword] = []
                else:
                    args, keywords = self._compile_arglist(trailer.children[1], trailer)
                atom = ast.Call(
                    func=atom,
                    args=args,
                    keywords=keywords,
                    **unify_line_ranges(
                        begin_range, get_line_range(trailer.children[-1])
                    ),
                )
            elif trailer.children[0].type == token.LSQB:  # subscript
                subscript = self.visit(trailer.children[1])
                atom = ast.Subscript(
                    value=atom,
                    slice=subscript,
                    ctx=self.expr_context,
                    **unify_line_ranges(
                        begin_range, get_line_range(trailer.children[-1])
                    ),
                )
            elif trailer.children[0].type == token.DOT:  # attribute
                atom = ast.Attribute(
                    value=atom,
                    attr=trailer.children[1].value,
                    ctx=self.expr_context,
                    **unify_line_ranges(
                        begin_range, get_line_range(trailer.children[1])
                    ),
                )
            else:
                raise NotImplementedError(repr(trailer))
        return atom

    def _compile_arglist(
        self, node: NL, parent_node: Node
    ) -> Tuple[List[ast.expr], List[ast.keyword]]:
        if not isinstance(node, Node) or node.type != syms.arglist:
            arguments = [node]
        else:
            arguments = node.children[::2]
        args: list[ast.expr] = []
        keywords: list[ast.keyword] = []
        for argument in arguments:
            if isinstance(argument, Leaf):
                arg = self.visit(argument)
                assert isinstance(arg, ast.expr)
                args.append(arg)
            elif argument.children[0].type == token.STAR:
                args.append(
                    ast.Starred(
                        value=self.visit(argument.children[1]),
                        ctx=ast.Load(),
                        **get_line_range(argument),
                    )
                )
            elif argument.children[0].type == token.DOUBLESTAR:
                keywords.append(
                    ast.keyword(
                        arg=None,
                        value=self.visit(argument.children[1]),
                        **get_line_range(argument),
                    )
                )
            elif len(argument.children) == 1:
                expr = self.visit(argument.children[0])
                assert isinstance(expr, ast.expr)
                args.append(expr)
            elif len(argument.children) == 2:
                inner = self.visit(argument.children[0])
                comps = self._compile_comprehension(argument.children[1])
                args.append(
                    ast.GeneratorExp(
                        elt=inner, generators=comps, **get_line_range(parent_node)
                    )
                )
            elif argument.children[1].type == token.COLONEQUAL:
                walrus = self._compile_named_expr(argument.children)
                if len(argument.children) > 3:
                    comps = self._compile_comprehension(node.children[3])
                    args.append(
                        ast.GeneratorExp(
                            elt=walrus, generators=comps, **get_line_range(parent_node)
                        )
                    )
                else:
                    args.append(walrus)
            elif argument.children[1].type == token.EQUAL:
                with self.set_expr_context(ast.Store()):
                    target = self.visit(argument.children[0])
                if not isinstance(target, ast.Name):
                    raise UnsupportedSyntaxError(
                        "keyword argument target must be a name"
                    )
                value = self.visit(argument.children[2])
                keywords.append(
                    ast.keyword(arg=target.id, value=value, **get_line_range(argument))
                )
            else:
                raise NotImplementedError(repr(argument))
        return args, keywords

    def _compile_comprehension(self, node: Node) -> List[ast.comprehension]:
        assert node.type in (syms.old_comp_for, syms.comp_for), repr(node)
        if node.children[0].type == token.ASYNC:
            is_async = 1
            children = node.children[1:]
        else:
            is_async = 0
            children = node.children
        if len(children) > 4:
            ifs, comps = self._compile_comp_iter(children[4])
        else:
            ifs = []
            comps = []
        with self.set_expr_context(ast.Store()):
            target = self.visit(children[1])
        comp = ast.comprehension(
            target=target, iter=self.visit(children[3]), ifs=ifs, is_async=is_async
        )
        return [comp, *comps]

    def _compile_comp_iter(
        self, node: Node
    ) -> Tuple[List[ast.expr], List[ast.comprehension]]:
        if node.children[0].value == "if":
            test = self.visit(node.children[1])
            assert isinstance(test, ast.expr)
            if len(node.children) > 2:
                ifs, comps = self._compile_comp_iter(node.children[2])
            else:
                ifs = []
                comps = []
            return [test, *ifs], comps
        else:
            return [], self._compile_comprehension(node)

    def _compile_named_expr(self, children: Sequence[NL]) -> ast.NamedExpr:
        with self.set_expr_context(ast.Store()):
            target = self.visit(children[0])
        if not isinstance(target, ast.Name):
            raise UnsupportedSyntaxError("walrus target must be a name")
        value = self.visit(children[2])
        return ast.NamedExpr(
            target=target,
            value=value,
            **unify_line_ranges(
                get_line_range(children[0]), get_line_range(children[2])
            ),
        )

    def visit_subscript(self, node: Node) -> ast.expr:
        if (
            len(node.children) == 3
            and isinstance(node.children[1], Leaf)
            and node.children[1].value == token.COLONEQUAL
        ):
            return self._compile_named_expr(node.children)
        consumer = _Consumer(node.children)
        if consumer.consume(token.COLON) is None:
            lower = self.visit(consumer.consume())
            assert consumer.consume(token.COLON) is not None
        else:
            lower = None
        if (sliceop := consumer.consume(syms.sliceop)) is not None:
            step = self.visit(sliceop.children[1])
            upper = None
        elif consumer.consume(token.COLON) is None:
            if (upper_node := consumer.consume()) is None:
                upper = None
            else:
                upper = self.visit(upper_node)
            if (sliceop := consumer.consume(syms.sliceop)) is not None:
                step = self.visit(sliceop.children[1])
            else:
                step = None
        else:
            upper = step = None
        return ast.Slice(lower=lower, upper=upper, step=step, **get_line_range(node))

    def visit_subscriptlist(self, node: Node) -> ast.Tuple:
        elts = [self.visit(child) for child in node.children[::2]]
        return ast.Tuple(elts=elts, ctx=self.expr_context, **get_line_range(node))

    # Leaves
    def visit_NAME(self, leaf: Leaf) -> ast.AST:
        return ast.Name(id=leaf.value, ctx=self.expr_context, **get_line_range(leaf))

    def visit_NUMBER(self, leaf: Leaf) -> ast.AST:
        return ast.Constant(value=ast.literal_eval(leaf.value), **get_line_range(leaf))

    def visit_STRING(self, leaf: Leaf) -> ast.AST:
        return ast.Constant(value=ast.literal_eval(leaf.value), **get_line_range(leaf))

    def visit_COLON(self, leaf: Leaf) -> ast.AST:
        return ast.Slice(lower=None, upper=None, step=None, **get_line_range(leaf))


def compile(code: str) -> ast.AST:
    return Compiler().visit(parse(code + "\n"))


if __name__ == "__main__":
    import sys

    code = sys.argv[1]
    print(ast.dump(compile(code)))
