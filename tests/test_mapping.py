"""Mapping language: one table entry per operator (typical, missing path, type error) plus grammar, caps, guards."""

from __future__ import annotations

import json
import random
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import mapping  # noqa: E402
from factory.mapping import MappingError, evaluate  # noqa: E402


def ev(expr, doc=None, ctx=None, **docs):
    value, _ = evaluate(expr, {"main": doc, **docs}, ctx or {})
    return value


P = lambda p: {"path": p}  # noqa: E731
DOC = {"a": {"b": 3, "s": " Hi ", "n": "12", "t": "x,y,z"}, "arr": [1, 2, 3], "items": [{"k": "a", "v": 1}, {"k": "b", "v": 2}],
       "html": "<p>One <b>two</b></p>", "f": 1.5, "ts": 1700000000}

# op -> (typical (expr, expected), missing (expr, expected), error expr)
TABLE = {
    "path": (P("a.b"), 3, P("a.x.y"), None, P("arr.k")),
    "lit": ({"lit": {"x": [1]}}, {"x": [1]}, {"lit": None}, None, {"lit": 1, "extra": 2}),
    "coalesce": ({"coalesce": [P("zz"), {"lit": ""}, {"lit": "x"}]}, "x", {"coalesce": [P("zz"), P("yy")]}, None, {"coalesce": 5}),
    "any": ({"any": [P("zz"), {"lit": True}]}, True, {"any": [P("zz")]}, False, {"any": 1}),
    "all": ({"all": [{"lit": True}, P("a.b")]}, True, {"all": [P("zz")]}, False, {"all": []}),
    "not": ({"not": {"lit": False}}, True, {"not": P("zz")}, True, {"not": 1, "x": 2}),
    "eq": ({"eq": [P("a.b"), {"lit": 3}]}, True, {"eq": [P("zz"), {"lit": 3}]}, False, {"eq": [1]}),
    "in": ({"in": [P("a.b"), [1, 3]]}, True, {"in": [P("zz"), [1]]}, False, {"in": [P("a.b"), 5]}),
    "exists": ({"exists": P("a")}, True, {"exists": P("zz")}, False, {"exists": {"lit": 1, "x": 1}}),
    "startswith": ({"startswith": [P("a.t"), "x,"]}, True, {"startswith": [P("zz"), "x"]}, False, {"startswith": [P("a.b"), "x"]}),
    "regex": ({"regex": [P("a.t"), "^x,"]}, True, {"regex": [P("zz"), "x"]}, False, {"regex": [P("a.t"), "("]}),
    "regex_group": ({"regex_group": [P("a.n"), "(?P<d>\\d)(\\d)", "d"]}, "1", {"regex_group": [P("zz"), "x", 0]}, None,
                    {"regex_group": [P("a.n"), "(\\d)", 4]}),
    "regex_sub": ({"regex_sub": [P("a.t"), ",", ";"]}, "x;y;z", {"regex_sub": [P("zz"), ",", ";"]}, None,
                  {"regex_sub": [P("a.t"), ",", "\\9"]}),
    "lower": ({"lower": {"lit": "AbC"}}, "abc", {"lower": P("zz")}, None, {"lower": P("a.b")}),
    "upper": ({"upper": {"lit": "AbC"}}, "ABC", {"upper": P("zz")}, None, {"upper": P("arr")}),
    "trim": ({"trim": P("a.s")}, "Hi", {"trim": P("zz")}, None, {"trim": P("a.b")}),
    "join": ({"join": [P("arr"), "-"]}, "1-2-3", {"join": [P("zz"), "-"]}, None, {"join": [P("a.s"), "-"]}),
    "split": ({"split": [P("a.t"), ","]}, ["x", "y", "z"], {"split": [P("zz"), ","]}, None, {"split": [P("a.t"), ""]}),
    "concat": ({"concat": ["n=", P("a.b"), "/", {"lit": "z"}]}, "n=3/z", {"concat": ["n=", P("zz")]}, None, {"concat": [P("arr")]}),
    "to_int": ({"to_int": P("a.n")}, 12, {"to_int": P("zz")}, None, {"to_int": {"lit": "abc"}}),
    "to_float": ({"to_float": {"lit": "2.5"}}, 2.5, {"to_float": P("zz")}, None, {"to_float": {"lit": "abc"}}),
    "to_bool": ({"to_bool": {"lit": "TRUE"}}, True, {"to_bool": P("zz")}, None, {"to_bool": {"lit": "maybe"}}),
    "date": ({"date": {"of": P("ts"), "formats": ["epoch_s"]}}, "2023-11-14T22:13:20Z",
             {"date": {"of": P("zz"), "formats": ["iso"]}}, None, {"date": {"of": {"lit": "not a date"}, "formats": ["iso"]}}),
    "enum": ({"enum": {"of": {"lit": "A"}, "map": {"A": "x"}}}, "x", {"enum": {"of": P("zz"), "map": {"A": "x"}}}, None,
             {"enum": {"of": {"lit": "Q"}, "map": {"A": "x"}}}),
    "object": ({"object": {"a": P("a.b"), "z": P("zz")}}, {"a": 3}, {"object": {"z": P("zz")}}, None, {"object": [1]}),
    "array": ({"array": [P("a.b"), P("zz"), {"lit": 2}]}, [3, 2], {"array": []}, [], {"array": {"x": 1}}),
    "each": ({"each": {"over": P("items"), "map": P("@.v"), "where": {"eq": [P("@.k"), {"lit": "b"}]}}}, [2],
             {"each": {"over": P("zz"), "map": P("@")}}, None, {"each": {"over": P("a.b"), "map": P("@")}}),
    "html_to_md": ({"html_to_md": P("html")}, "One **two**", {"html_to_md": P("zz")}, None, {"html_to_md": P("a.b")}),
    "text_from_tree": ({"text_from_tree": {"of": {"lit": {"type": "p", "content": [{"type": "text", "text": "hi"}]}},
                                           "config": {"block": {"paragraph": "p"}}}}, "hi",
                       {"text_from_tree": {"of": P("zz"), "config": {}}}, None,
                       {"text_from_tree": {"of": {"lit": {"type": "p"}}, "config": {"bogus": 1}}}),
    "tree": ({"tree": {"root": {"lit": {"i": 1, "c": [{"i": 2}]}}, "children": "c", "map": {"object": {"id": P("@.i")}}}},
             {"id": 1, "children": [{"id": 2}]}, {"tree": {"root": P("zz"), "children": "c", "map": {"lit": {}}}}, None,
             {"tree": {"root": {"lit": {"c": 5}}, "children": "c", "map": {"lit": {"a": 1}}}}),
    "rgb_hex": ({"rgb_hex": {"r": {"lit": 1}, "g": {"lit": 0.5}, "b": {"lit": 0}}}, "#ff8000",
                {"rgb_hex": {"r": P("zz"), "g": {"lit": 1}, "b": {"lit": 1}}}, None,
                {"rgb_hex": {"r": {"lit": "x"}, "g": {"lit": 1}, "b": {"lit": 1}}}),
    "if": ({"if": {"cond": P("a.b"), "then": {"lit": "y"}, "else": {"lit": "n"}}}, "y",
           {"if": {"cond": P("zz"), "then": {"lit": "y"}}}, None, {"if": {"cond": P("a.b")}}),
    "type_is": ({"type_is": [P("arr"), "array"]}, True, {"type_is": [P("zz"), "null"]}, True,
                {"type_is": [P("arr"), "list"]}),
}


class OperatorTests(unittest.TestCase):
    def test_table_covers_every_operator(self):
        self.assertEqual(set(TABLE), set(mapping.OPS))

    def test_each_operator_typical_missing_and_error(self):
        for op, (typ, want, miss, miss_want, bad) in TABLE.items():
            with self.subTest(op=op, case="typical"):
                self.assertEqual(ev(typ, DOC), want)
            with self.subTest(op=op, case="missing"):
                self.assertEqual(ev(miss, DOC), miss_want)
            if bad is not None:
                with self.subTest(op=op, case="error"):
                    with self.assertRaises(MappingError):
                        ev(bad, DOC)
        with self.subTest(op="path-lit-error-shapes"):
            with self.assertRaises(MappingError):
                ev({"nope": 1}, DOC)

    def test_rgb_hex_scales_clamp_and_lint(self):
        def rgb(r, g, b, **kw):
            return ev({"rgb_hex": {"r": {"lit": r}, "g": {"lit": g}, "b": {"lit": b}, **kw}})
        self.assertEqual(rgb(0, 1, 0.2), "#00ff33")
        self.assertEqual(rgb(1.5, -1, 0.5), "#ff0080")
        self.assertEqual(rgb(255, 16, 0, scale={"lit": 255}), "#ff1000")
        self.assertIsNone(rgb(None, 1, 1))
        with self.assertRaises(MappingError):
            rgb(1, 1, 1, scale={"lit": 2})
        self.assertEqual(mapping.lint_expr({"rgb_hex": {"r": 1, "g": 1, "b": 1, "scale": 7}}), ["/rgb_hex: scale must be 1 or 255"])
        self.assertEqual(mapping.lint_expr({"rgb_hex": {"r": 1, "g": 1}}), ["/rgb_hex: missing ['b']"])

    def test_if_and_type_is(self):
        self.assertEqual(ev({"if": {"cond": {"lit": 0}, "then": {"lit": 1}, "else": {"lit": 2}}}), 2)
        self.assertIsNone(ev({"if": {"cond": {"lit": False}, "then": {"lit": 1}}}))
        for val, name in (([1], "array"), ({"a": 1}, "object"), ("s", "string"), (1.5, "number"), (True, "bool"), (None, "null")):
            self.assertTrue(ev({"type_is": [{"lit": val}, name]}), name)
        self.assertFalse(ev({"type_is": [{"lit": True}, "number"]}))
        self.assertEqual(mapping.lint_expr({"type_is": [{"lit": 1}, "list"]}),
                         ["/type_is: type must be one of ['array', 'object', 'string', 'number', 'bool', 'null']"])
        self.assertEqual(mapping.lint_expr({"if": {"cond": {"bogus": 1}, "then": 1}}), ["/if/cond/bogus: unknown operator 'bogus'"])

    def test_error_names_operator_value_and_pointer(self):
        with self.assertRaises(MappingError) as cm:
            evaluate({"object": {"n": {"to_int": {"lit": "x" * 200}}}}, {"main": {}}, {}, pointer="/kinds/ticket/map/n")
        msg = str(cm.exception)
        self.assertIn("to_int", msg)
        self.assertIn("/kinds/ticket/map/n/object/n/to_int", msg)
        self.assertLess(len(msg), 400)  # input truncated to 80 characters
        self.assertEqual(cm.exception.op, "to_int")

    def test_strict_bool_and_number_equality(self):
        self.assertFalse(ev({"eq": [{"lit": True}, {"lit": 1}]}))
        self.assertTrue(ev({"eq": [{"lit": 1}, {"lit": 1.0}]}))
        self.assertFalse(ev({"in": [{"lit": "A"}, ["a"]]}))  # case-sensitive unless wrapped in lower
        self.assertTrue(ev({"in": [{"lower": {"lit": "A"}}, ["a"]]}))

    def test_to_bool_accepts_documented_forms(self):
        for raw, want in ((True, True), ("true", True), ("false", False), (0, False), (1, True)):
            self.assertEqual(ev({"to_bool": {"lit": raw}}), want)
        with self.assertRaises(MappingError):
            ev({"to_bool": {"lit": 2}})

    def test_date_formats(self):
        self.assertEqual(ev({"date": {"of": {"lit": 1700000000000}, "formats": ["epoch_ms"]}}), "2023-11-14T22:13:20Z")
        self.assertEqual(ev({"date": {"of": {"lit": "2023-11-14T23:13:20+01:00"}, "formats": ["iso"]}}), "2023-11-14T22:13:20Z")
        self.assertEqual(ev({"date": {"of": {"lit": "14/11/2023 22:13"}, "formats": ["iso", "%d/%m/%Y %H:%M"]}}), "2023-11-14T22:13:00Z")
        self.assertEqual(ev({"date": {"of": {"lit": "2023-11-14T22:13:20Z"}, "formats": ["iso"]}}), "2023-11-14T22:13:20Z")

    def test_enum_default_and_unmapped_error(self):
        self.assertEqual(ev({"enum": {"of": {"lit": "zzz"}, "map": {"A": "x"}, "default": "other"}}), "other")
        self.assertEqual(ev({"enum": {"of": {"lit": 3}, "map": {"3": "three"}}}), "three")
        with self.assertRaises(MappingError):
            ev({"enum": {"of": {"lit": "zzz"}, "map": {"A": "x"}}})

    def test_object_with_only_nulls_is_null_and_each_drops_nulls(self):
        self.assertIsNone(ev({"object": {"a": P("zz")}}))
        self.assertEqual(ev({"each": {"over": P("items"), "map": {"path": "@.nope"}}}, DOC), [])

    def test_each_flatten_and_parent_scope(self):
        doc = {"groups": [{"n": "g1", "xs": [1, 2]}, {"n": "g2", "xs": [3]}]}
        flat = {"each": {"over": P("groups"), "map": P("@.xs"), "flatten": True}}
        self.assertEqual(ev(flat, doc), [1, 2, 3])
        nested = {"each": {"over": P("groups"), "map": {"each": {"over": P("@.xs"), "map": {"concat": [P("@@.n"), ":", P("@")]}}}, "flatten": True}}
        self.assertEqual(ev(nested, doc), ["g1:1", "g1:2", "g2:3"])
        with self.assertRaises(MappingError):
            ev({"each": {"over": P("groups"), "map": P("@.n"), "flatten": True}}, doc)
        with self.assertRaises(MappingError):
            ev(P("@@.n"), doc)  # outside a nested each

    def test_each_item_error_fails_whole_contract(self):
        with self.assertRaises(MappingError):
            ev({"each": {"over": P("arr"), "map": {"to_int": {"lit": "x"}}}}, DOC)


class PathTests(unittest.TestCase):
    def test_grammar(self):
        d = {"a.b": 1, "x": {"y": [10, 20, 30]}, "rows": [{"t": "k", "v": 1}, {"t": "m", "v": 2}, {"t": "k", "v": 3}],
             "q": {"it's": 5}}
        cases = {
            '["a.b"]': 1, "x.y[0]": 10, "x.y[-1]": 30, "x.y[5]": None, "x.y[*]": [10, 20, 30],
            "rows[*].v": [1, 2, 3], "rows[?t==k].v": [1, 3], "rows[?t!=k].v": [2], 'rows[?t=="m"].v': [2],
            "rows[?v==3].t": ["k"], "rows[?t==k][0].v": 1, "rows[?t==k][-1].v": 3, "rows[?t==zz][0].v": None,
            "x.nope.deeper": None, "nope[*]": [], "nope[?t==k]": [], "nope[0]": None, "$.x.y[1]": 20, "$": d, "q[\"it's\"]": 5,
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(ev(P(path), d), want)

    def test_type_errors(self):
        d = {"s": "text", "n": 3, "arr": [1], "o": {"k": 1}}
        for path in ("s.k", "n.k", "arr.k", "s[0]", "o[0]", "s[*]", "o[*]", "arr[?k==1]", "n[?k==1]"):
            with self.subTest(path=path), self.assertRaises(MappingError):
                ev(P(path), d)

    def test_bad_paths(self):
        for path in ("a..b", "a.", "a[", "a[x]", "a[?k]", '["unterminated]', "a[?k=="):
            with self.subTest(path=path), self.assertRaises(MappingError):
                ev(P(path), {"a": [1]})

    def test_scopes(self):
        out = ev({"object": {"m": P("v"), "j": P("$jobs.x"), "c": P("$ctx.repo"), "root": P("$.v")}}, {"v": 1},
                 {"repo": "api"}, jobs={"x": 2})
        self.assertEqual(out, {"m": 1, "j": 2, "c": "api", "root": 1})
        with self.assertRaises(MappingError):
            ev(P("$nothere.x"), {})
        with self.assertRaises(MappingError):
            ev(P("@.x"), {})

    def test_fuzz_never_escapes_as_another_exception(self):
        rnd = random.Random(7)
        alphabet = list("ab.[]*?=!'\"0-1$@ ")
        doc = {"a": [{"b": 1}, None, {"b": [1, 2]}], "b": {"a": "s"}}
        for _ in range(3000):
            path = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
            try:
                ev(P(path), doc)
            except MappingError:
                pass


class DeterminismAndErrorsTests(unittest.TestCase):
    def test_same_input_same_bytes(self):
        mp = {"x": {"each": {"over": P("items"), "map": {"object": {"k": P("@.k"), "v": {"to_float": P("@.v")}}}}},
              "h": {"html_to_md": P("html")}}
        a = mapping.evaluate_map(mp, {"main": DOC}, {})
        b = mapping.evaluate_map(mp, {"main": DOC}, {})
        self.assertEqual(json.dumps(a), json.dumps(b))

    def test_evaluate_map_collects_every_field_error_with_pointer(self):
        values, unknowns, errors = mapping.evaluate_map(
            {"ok": P("a.b"), "bad1": {"to_int": {"lit": "q"}}, "bad2": {"nope": 1}}, {"main": DOC}, {}, "/kinds/t/map")
        self.assertEqual(values, {"ok": 3})
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("/kinds/t/map/bad1" in e for e in errors))
        self.assertTrue(any("/kinds/t/map/bad2" in e for e in errors))

    def test_no_io_or_time_operators(self):
        for forbidden in ("now", "random", "read", "exec", "eval", "import"):
            self.assertNotIn(forbidden, mapping.OPS)
            with self.assertRaises(MappingError):
                ev({forbidden: 1})

    def test_lint_expr(self):
        self.assertEqual(mapping.lint_expr({"to_int": P("a")}), [])
        self.assertTrue(mapping.lint_expr({"frob": 1}))
        self.assertTrue(mapping.lint_expr({"regex": [P("a"), "("]}))
        self.assertTrue(mapping.lint_expr({"regex": [P("a"), "(a+)+"]}))
        self.assertTrue(mapping.lint_expr({"each": {"over": P("a")}}))
        self.assertTrue(mapping.lint_expr({"eq": [1]}))
        self.assertTrue(mapping.lint_expr({"object": {"x": {"frob": 1}}}))
        self.assertTrue(mapping.lint_expr({"enum": {"of": P("a"), "map": []}}))
        self.assertTrue(mapping.lint_expr({"path": "a..b"}))


class CapTests(unittest.TestCase):
    @staticmethod
    def chain(depth):
        node = {"i": 0}
        top = node
        for n in range(1, depth):
            node["c"] = [{"i": n}]
            node = node["c"][0]
        return top

    def tree(self, raw):
        return ev({"tree": {"root": {"lit": raw}, "children": "c", "map": {"object": {"id": P("@.i")}}}})

    def test_depth_cap(self):
        self.assertIsNotNone(self.tree(self.chain(mapping.TREE_MAX_DEPTH)))
        with self.assertRaises(MappingError) as cm:
            self.tree(self.chain(mapping.TREE_MAX_DEPTH + 1))
        self.assertIn("depth cap", str(cm.exception))

    def test_node_cap(self):
        wide = {"i": 0, "c": [{"i": n} for n in range(mapping.TREE_MAX_NODES - 1)]}
        self.assertIsNotNone(self.tree(wide))
        wide["c"].append({"i": "extra"})
        with self.assertRaises(MappingError) as cm:
            self.tree(wide)
        self.assertIn("node cap", str(cm.exception))

    def test_rich_text_node_cap(self):
        doc = {"type": "d", "content": [{"type": "p", "content": []} for _ in range(mapping.TREE_MAX_NODES + 1)]}
        with self.assertRaises(MappingError):
            ev({"text_from_tree": {"of": {"lit": doc}, "config": {"block": {"paragraph": "p"}}}})


class RegexGuardTests(unittest.TestCase):
    def test_catastrophic_shapes_are_rejected_without_running(self):
        t0 = time.monotonic()
        for pat in ("(a+)+$", "(a*)*b", "((a+)?)+", "(\\w+\\s?)+$", "(x{1,50})+y"):
            with self.subTest(pat=pat), self.assertRaises(MappingError) as cm:
                ev({"regex": [{"lit": "a" * 60 + "!"}, pat]})
            self.assertIn("repeat", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_ordinary_patterns_pass(self):
        for pat in ("^(?i:draft:\\s*)", "(?P<key>[A-Z]+-\\d+)", "(a|b)*c", "x{1,5}y+", "\\s+$"):
            with self.subTest(pat=pat):
                mapping.compile_regex(pat)

    def test_input_and_pattern_caps(self):
        with self.assertRaises(MappingError):
            ev({"regex": [{"lit": "a" * (mapping.REGEX_MAX_INPUT + 1)}, "a"]})
        with self.assertRaises(MappingError):
            mapping.compile_regex("a" * (mapping.REGEX_MAX_PATTERN + 1))

    def test_slow_match_trips_the_wall_clock_guard(self):
        old = mapping.REGEX_BUDGET_S
        mapping.REGEX_BUDGET_S = -1.0  # any call is "too slow"
        try:
            with self.assertRaises(MappingError) as cm:
                ev({"regex": [{"lit": "abc"}, "b"]})
            self.assertIn("guard", str(cm.exception))
        finally:
            mapping.REGEX_BUDGET_S = old


class RichTextTests(unittest.TestCase):
    CFG = {"text_node": "text", "text_key": "t", "children_key": "c",
           "block": {"paragraph": "p", "heading": "h", "bullet_list": "ul", "ordered_list": "ol", "list_item": "li",
                     "code_block": "pre", "blockquote": "quote"},
           "marks": {"strong": "**", "em": "_", "code": "`"}, "link": {"type": "link", "url_key": "attrs.href"},
           "mention": {"type": "mention", "text_key": "attrs.text"}, "unknown_node": "descend"}

    def run_tree(self, tree, cfg=None):
        return evaluate({"text_from_tree": {"of": {"lit": tree}, "config": cfg or self.CFG}}, {"main": {}}, {})

    def test_golden_document(self):
        txt = lambda s, *m: {"type": "text", "t": s, **({"marks": [{"type": x} for x in m]} if m else {})}  # noqa: E731
        tree = {"type": "doc", "c": [
            {"type": "h", "attrs": {"level": 2}, "c": [txt("Title")]},
            {"type": "p", "c": [txt("a "), txt("b", "strong"), txt(" "), txt("c", "em", "code"), {"type": "mention", "attrs": {"text": "@ana"}},
                                {"type": "link", "attrs": {"href": "https://x.example"}, "c": [txt("site")]}]},
            {"type": "ul", "c": [{"type": "li", "c": [{"type": "p", "c": [txt("one")]}]},
                                 {"type": "li", "c": [{"type": "p", "c": [txt("two")]},
                                                      {"type": "ol", "c": [{"type": "li", "c": [{"type": "p", "c": [txt("deep")]}]}]}]}]},
            {"type": "pre", "c": [txt("x = 1")]},
            {"type": "quote", "c": [{"type": "p", "c": [txt("quoted")]}]}]}
        out, unknowns = self.run_tree(tree)
        self.assertEqual(out, "## Title\n\na **b** `_c_`@ana[site](https://x.example)\n\n- one\n- two\n  1. deep\n\n```\nx = 1\n```\n\n> quoted")
        self.assertEqual(unknowns, [])

    def test_unknown_node_descends_and_is_recorded(self):
        out, unknowns = self.run_tree({"type": "doc", "c": [{"type": "panel", "c": [{"type": "p", "c": [{"type": "text", "t": "in"}]}]}]})
        self.assertEqual(out, "in")
        self.assertEqual([u["reason"] for u in unknowns], ["rich-text node type 'panel' descended without config"])

    def test_unknown_node_error_mode(self):
        with self.assertRaises(MappingError):
            self.run_tree({"type": "doc", "c": [{"type": "panel", "c": []}]}, dict(self.CFG, unknown_node="error"))

    def test_html_to_md(self):
        src = ("<h2>Head</h2><p>Hello <strong>bold</strong> <em>it</em> <a href=\"https://x.example\">link</a><br>next</p>"
               "<ul><li>a</li><li>b<ol><li>c</li></ol></li></ul><pre>code\n  here</pre><blockquote><p>q</p></blockquote>"
               "<table><tr><th>h1</th><th>h2</th></tr><tr><td>1</td><td>2</td></tr></table><img src=\"i.png\" alt=\"pic\">"
               "<script>evil()</script>")
        md, extra = mapping.html_to_md(src)
        self.assertEqual(extra, [])
        for want in ("## Head", "Hello **bold** _it_ [link](https://x.example)\nnext", "- a", "- b", "  1. c", "```\ncode\n  here\n```",
                     "> q", "h1 | h2", "1 | 2", "![pic](i.png)"):
            self.assertIn(want, md)
        self.assertNotIn("evil", md)

    def test_html_unknown_tags_become_unknowns(self):
        _, unknowns = evaluate({"html_to_md": {"lit": "<p>x</p><marquee>y</marquee>"}}, {"main": {}}, {})
        self.assertEqual([u["reason"] for u in unknowns], ["html tag <marquee> rendered as text"])


if __name__ == "__main__":
    unittest.main()
