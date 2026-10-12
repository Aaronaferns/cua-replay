"""Commit-route matching: does this request commit?

The profile lists the requests that change the application's data (`Profile.commit_routes`). This pure matcher is
what a network-level guard uses to decide that a request is a commit, whatever the capability claims about its steps:
commit routes come from the profile and only ever raise an effect class (a regex or heuristic may add more, never
fewer, and nothing can lower a match).

Matching is by method, path (query dropped), required query parameters and required form fields. Every constraint is a
`*` glob over the whole value, case-sensitive. Query parameters and form fields not named by the pattern are ignored.

Leaf module: standard library plus this package.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit

from cua.artifact.predicates import glob_match
from cua.artifact.schema import Profile, RoutePattern


def route_matches(
    pattern: RoutePattern, method: str, url: str, form_fields: Mapping[str, str] | None = None
) -> bool:
    """`form_fields=None` means the body is unknown (not yet read, or not a form): body constraints then count as
    satisfied, because a request that might commit must be treated as one."""
    if pattern.method != "ANY" and pattern.method != method.upper():
        return False
    parts = urlsplit(url)
    if not glob_match(pattern.path, parts.path or "/"):
        return False
    query = parse_qsl(parts.query, keep_blank_values=True)
    for key, want in pattern.query.items():
        if not any(k == key and glob_match(want, v) for k, v in query):
            return False
    if form_fields is not None:
        for key, want in pattern.body.items():
            if key not in form_fields or not glob_match(want, form_fields[key]):
                return False
    return True


def is_commit(
    profile: Profile, method: str, url: str, form_fields: Mapping[str, str] | None = None
) -> bool:
    return any(route_matches(r, method, url, form_fields) for r in profile.commit_routes)
