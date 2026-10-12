"""The closed predicate language, the outline reader and the classifier built on them."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from cua.artifact.classify import classify
from cua.artifact.predicates import (
    AllOf,
    AnyOf,
    Not,
    Predicate,
    ScreenNode,
    ScreenView,
    TargetEqualsInput,
    TargetVisible,
    Text,
    UrlMatches,
    clause_score,
    holds,
    is_static,
    url_path,
    walk,
)
from cua.artifact.screen import parse_outline, view_from_outline
from cua.artifact.schema import Profile, State

PRED = TypeAdapter(Predicate)


def view(url: str = "http://h:1/a/b.php?x=1", *nodes: ScreenNode) -> ScreenView:
    return ScreenView(url=url, nodes=tuple(nodes))


def test_seven_forms_and_nothing_else():
    forms = {"url_matches", "text", "target_visible", "target_equals_input", "all", "any", "not"}
    mapping = PRED.json_schema()["discriminator"]["mapping"]
    assert set(mapping) == forms
    with pytest.raises(ValidationError):
        PRED.validate_python({"kind": "eval", "code": "1"})
    with pytest.raises(ValidationError):
        PRED.validate_python({"kind": "url_matches", "pattern": "/x", "regex": True})


def test_url_matches_ignores_origin_and_uses_a_single_wildcard():
    v = view("https://other-host:9/societe/list.php?type=c&x=2")
    assert holds(UrlMatches(pattern="/societe/list.php*"), v)
    assert holds(UrlMatches(pattern="/societe/list.php?type=c*"), v)
    assert not holds(UrlMatches(pattern="/societe/card.php*"), v)
    assert not holds(UrlMatches(pattern="/societe/list"), v)  # anchored: no implicit prefix
    assert url_path("http://h/") == "/"


def test_text_equals_is_normalized_exact_and_never_a_substring():
    v = view("http://h/", ScreenNode("cell", name="Customer Code"), ScreenNode("generic", text="Third party"))
    assert holds(Text(equals="customer code:"), v)
    assert holds(Text(equals="Third  party"), v)  # matches the text of a node without a name
    assert not holds(Text(equals="Customer"), v)
    assert not holds(Text(equals="Code"), v)


def test_text_role_and_frame_restrict_the_match():
    v = view("http://h/", ScreenNode("cell", name="Login", frame="f1"), ScreenNode("button", name="Go"))
    assert holds(Text(equals="Login", role="cell"), v)
    assert not holds(Text(equals="Login", role="textbox"), v)
    assert holds(Text(equals="Login", frame="f1"), v)
    assert not holds(Text(equals="Login", frame="f2"), v)


def test_text_pattern_is_anchored_over_normalized_text():
    v = view("http://h/", ScreenNode("cell", name="Customers (28)"))
    assert holds(Text(pattern=r"customers \(\d+\)"), v)
    assert not holds(Text(pattern=r"customers"), v)  # full match only
    assert not holds(Text(pattern=r"\d+"), v)


def test_text_needs_exactly_one_matcher():
    with pytest.raises(ValidationError):
        Text()
    with pytest.raises(ValidationError):
        Text(equals="a", pattern="a")
    with pytest.raises(ValidationError):
        Text(equals=" : ")  # empty after normalization would match every unlabeled node
    with pytest.raises(ValidationError) as e:  # a bad regex is a validation error that does not echo it
        Text(pattern="(SECRET")
    assert "SECRET" not in str(e.value)


def test_combinators():
    a, b = Text(equals="a"), Text(equals="b")
    v = view("http://h/", ScreenNode("generic", name="a"))
    assert holds(AnyOf(of=[a, b]), v)
    assert not holds(AllOf(of=[a, b]), v)
    assert holds(Not(of=b), v)
    assert holds(AllOf(of=[a, Not(of=b)]), v)
    with pytest.raises(ValidationError):
        AllOf(of=[])


def test_target_predicates_are_not_static():
    tv = TargetVisible(target="t")
    te = TargetEqualsInput(target="t", input="i")
    assert not is_static(AllOf(of=[Text(equals="a"), tv]))
    assert is_static(AllOf(of=[Text(equals="a"), UrlMatches(pattern="/x*")]))
    with pytest.raises(TypeError):
        holds(te, view())
    assert [n.kind for n in walk(AllOf(of=[tv, Not(of=te)]))] == ["all", "target_visible", "not", "target_equals_input"]


def test_clause_score_counts_top_level_clauses():
    sig = AllOf(of=[UrlMatches(pattern="/a*"), Text(equals="x"), Text(equals="y")])
    assert clause_score(sig, view("http://h/a", ScreenNode("generic", name="x"))) == (2, 3)


# --- outline reader --------------------------------------------------------------------------------------------

OUTLINE = """
- generic [ref=e3]:
  - generic "Dolibarr 23.0.4" [ref=e5]:
    - link "Dolibarr 23.0.4" [ref=e6] [cursor=pointer]:
      - /url: https://www.dolibarr.org
  - textbox "Login" [active] [ref=e15]
  - text: Third party
  - generic [ref=e299]: CU1212-0005
  - 'generic "Email : a@b.c" [ref=e286]': ""
  - 'cell "Account by default: 411" [ref=e308]'
  - generic [ref=e207]: "1"
  - link "He said \\"hi\\"" [ref=e9]
  - not a node line
"""


def test_parse_outline_reads_roles_names_and_text():
    nodes = parse_outline(OUTLINE)
    by = [(n.role, n.name, n.text) for n in nodes]
    assert ("textbox", "Login", None) in by
    assert ("text", None, "Third party") in by
    assert ("generic", None, "CU1212-0005") in by
    assert ("generic", "Email : a@b.c", None) in by
    assert ("cell", "Account by default: 411", None) in by
    assert ("generic", None, "1") in by
    assert ("link", 'He said "hi"', None) in by
    assert not any(n.role.startswith("/") for n in nodes)  # property lines are skipped


def test_parse_outline_marks_frame():
    assert all(n.frame == "f2" for n in parse_outline(OUTLINE, frame="f2"))


# --- classifier ------------------------------------------------------------------------------------------------


def _state(sig, specializes=None, layer="page") -> State:
    return State(description="t", signature=sig, specializes=specializes, origin="declared", layer=layer)


@pytest.fixture
def profile() -> Profile:
    from sample_docs import dolibarr_profile

    return dolibarr_profile()


def screen(url: str, outline: str) -> ScreenView:
    return view_from_outline(url, outline)


def test_login_form_at_an_app_url_is_classified_by_content(profile):
    form = '- textbox "Login" [ref=e1]\n- textbox "Password" [ref=e2]\n- button "Login" [ref=e3]\n'
    for url in ("http://h/index.php?mainmenu=home", "http://h/societe/list.php?type=c", "http://h/societe/card.php?socid=1"):
        assert classify(profile, screen(url, form)).label == "login"


def test_pages_classify_by_url_and_content(profile):
    home = screen("http://h/index.php?mainmenu=home&leftmenu=home", '- generic [ref=e1]: Global view\n')
    lst = screen("http://h/societe/list.php?type=c", '- columnheader "Third-party name" [ref=e1]\n')
    card = screen("http://h/societe/card.php?socid=7", '- cell "Customer Code" [ref=e1]\n')
    assert [classify(profile, v).label for v in (home, lst, card)] == ["home", "customer_list", "customer_card"]


def test_unknown_screen_carries_near_misses(profile):
    # right URL, none of the content
    c = classify(profile, screen("http://h/societe/list.php", '- generic [ref=e1]: Something else\n'))
    assert c.kind == "unknown" and c.state is None
    assert c.near_misses and c.near_misses[0].startswith("customer_list (1/2)")


def test_specialization_wins_over_its_parent():
    sig_parent = Text(equals="Customers")
    sig_child = AllOf(of=[Text(equals="Customers"), Text(equals="No record found")])
    p = _profile_with({"customer_list": _state(sig_parent),
                       "customer_not_found": _state(sig_child, specializes="customer_list")})
    v = screen("http://h/x", '- generic "Customers"\n- cell "No record found"\n')
    assert classify(p, v).state == "customer_not_found"
    assert classify(p, screen("http://h/x", '- generic "Customers"\n')).state == "customer_list"


def test_unrelated_overlap_is_ambiguous():
    p = _profile_with({"a": _state(Text(equals="x")), "b": _state(Text(equals="x"))})
    c = classify(p, screen("http://h/", '- generic "x"\n'))
    assert c.kind == "ambiguous" and c.candidates == ("a", "b") and c.label == "ambiguous"


def test_dialog_layer_is_matched_first_and_alone():
    p = _profile_with({
        "list": _state(Text(equals="Customers")),
        "confirm": _state(Text(equals="Are you sure?"), layer="dialog"),
    })
    page = screen("http://h/", '- generic "Customers"\n')
    dialog = screen("http://h/", '- generic "Are you sure?"\n')
    assert classify(p, page, dialog).state == "confirm"
    assert classify(p, page).state == "list"
    # an unrecognised dialog is unknown, never the page underneath it
    other = screen("http://h/", '- generic "Something unexpected"\n')
    assert classify(p, page, other).kind == "unknown"


def _profile_with(states: dict[str, State]) -> Profile:
    from sample_docs import dolibarr_profile

    base = dolibarr_profile()
    first = next(iter(states))
    return base.model_copy(update={
        "states": states,
        "entry": base.entry.model_copy(update={"start_state": first}),
        "login": base.login.model_copy(update={"state": first}),
        "default_meaning": {},
    })
