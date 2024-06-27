import ast

from lib2toast.compile import compile


def assert_compiles(code: str) -> None:
    our_code = compile(code)
    ast_code = ast.parse(code)
    assert ast.dump(our_code, include_attributes=True) == ast.dump(ast_code, include_attributes=True)


def test_name() -> None:
    assert_compiles("a")