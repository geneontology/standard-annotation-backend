"""Verify the complete users.yaml authorization boundary before any writes."""

import pytest
import yaml

from standard_annotation_backend.auth import users_yaml as parser


def _parse(payload: object) -> parser.UsersDocument:
    return parser.parse_users_yaml(yaml.safe_dump(payload))


def test_parses_and_normalizes_all_supported_authorization_contexts() -> None:
    """Ordinary, trainee, multi-group, and global grants retain distinct contexts."""
    document = parser.parse_users_yaml("""
- nickname: Curator
  accounts: {github: Some-Curator}
  groups: [legacy-group]
  authorizations:
    noctua: {go: {allow-edit: true}}
    sab:
      - {role: edit, scope: group, group: zfin}
      - {role: edit, scope: group, group: mgi}
      - {role: edit, scope: self, group: zfin}
      - {role: admin, scope: global}
      - {role: read, scope: group, group: zfin}
      - {role: edit, scope: group, group: zfin}
""")
    user = document.root[0]
    assert user.accounts.github == "some-curator"
    assert user.nickname == "Curator"
    assert [grant.model_dump(mode="json") for grant in user.authorizations.sab] == [
        {"role": "admin", "scope": "global", "group": None},
        {"role": "edit", "scope": "group", "group": "mgi"},
        {"role": "edit", "scope": "group", "group": "zfin"},
        {"role": "edit", "scope": "self", "group": "zfin"},
        {"role": "read", "scope": "group", "group": "zfin"},
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"nickname": "Legacy user"},
        {"accounts": {"github": "curator"}, "groups": ["legacy-group"]},
        {"authorizations": {"noctua": {"go": {"allow-admin": True}}}},
        {"authorizations": {"sab": []}},
    ],
)
def test_legacy_entries_do_not_create_sab_grants(entry: object) -> None:
    """Users without SAB authorization remain valid and receive no grants."""
    document = parser.parse_users_yaml(yaml.safe_dump([entry]))
    assert document.root[0].authorizations.sab == ()


@pytest.mark.parametrize(
    ("grant", "issue_type"),
    [
        ({"role": "owner", "scope": "global"}, "enum"),
        ({"role": "edit", "scope": "organization", "group": "zfin"}, "enum"),
        ({"role": "edit", "scope": "group"}, "group_required"),
        ({"role": "edit", "scope": "self"}, "group_required"),
        ({"role": "edit", "scope": "group", "group": None}, "group_required"),
        ({"role": "edit", "scope": "group", "group": " "}, "string_too_short"),
        ({"role": "admin", "scope": "global", "group": "zfin"}, "group_prohibited"),
        ({"role": "admin", "scope": "global", "group": None}, "group_prohibited"),
        ({"role": "admin", "scope": "global", "groups": ["zfin"]}, "extra_forbidden"),
        ({"role": "admin"}, "missing"),
    ],
)
def test_rejects_invalid_grants(grant: object, issue_type: str) -> None:
    """Invalid grants report stable issue codes and the failing input path."""
    with pytest.raises(parser.InvalidUsersDocumentError) as caught:
        _parse(
            [{"accounts": {"github": "curator"}, "authorizations": {"sab": [grant]}}]
        )
    issue = caught.value.errors[0]
    assert issue["type"] == issue_type
    assert issue["location"][:4] == (0, "authorizations", "sab", 0)
    assert set(issue) == {"type", "location", "message"}


@pytest.mark.parametrize(
    "accounts", [{}, {"github": None}, {"github": ""}, {"github": " "}, {"github": 123}]
)
def test_sab_grants_require_a_nonblank_string_github_login(accounts: object) -> None:
    """Every SAB grant is attached to a valid user GitHub identity."""
    with pytest.raises(parser.InvalidUsersDocumentError):
        _parse(
            [
                {
                    "accounts": accounts,
                    "authorizations": {"sab": [{"role": "admin", "scope": "global"}]},
                }
            ]
        )


def test_legacy_groups_cannot_supply_a_missing_sab_group() -> None:
    """Only a grant's explicit group establishes SAB ownership."""
    with pytest.raises(parser.InvalidUsersDocumentError) as caught:
        _parse(
            [
                {
                    "accounts": {"github": "curator"},
                    "groups": ["zfin"],
                    "authorizations": {"sab": [{"role": "edit", "scope": "group"}]},
                }
            ]
        )
    assert caught.value.errors[0]["type"] == "group_required"


def test_rejects_duplicate_case_insensitive_github_identities() -> None:
    """Duplicate user identities cannot ambiguously merge their permissions."""
    with pytest.raises(parser.InvalidUsersDocumentError) as caught:
        _parse(
            [{"accounts": {"github": "Curator"}}, {"accounts": {"github": "curator"}}]
        )
    assert caught.value.errors[0]["type"] == "duplicate_github_login"


@pytest.mark.parametrize(
    "source",
    [
        "[",
        "- accounts: {github: curator}\n  accounts: {github: other}\n",
        "!!python/object:some.Class {}",
    ],
)
def test_rejects_malformed_unsafe_or_ambiguous_yaml(source: str) -> None:
    """Invalid YAML, unsafe tags, and duplicate mapping keys fail closed."""
    with pytest.raises(parser.InvalidUsersDocumentError) as caught:
        parser.parse_users_yaml(source)
    assert caught.value.errors[0]["type"] == "invalid_yaml"
    assert caught.value.errors[0]["location"] == ()


@pytest.mark.parametrize(
    "payload", [None, {}, {"users": []}, [None], [{"authorizations": {"sab": None}}]]
)
def test_rejects_invalid_document_shapes(payload: object) -> None:
    """An invalid document cannot silently become an empty replacement."""
    with pytest.raises(parser.InvalidUsersDocumentError):
        _parse(payload)


def test_accepts_explicit_empty_document() -> None:
    """An explicit empty sequence represents removal of all current grants."""
    assert parser.parse_users_yaml("[]").root == ()


@pytest.mark.parametrize(
    ("source", "location"),
    [
        ("!!set {}", ()),
        ("!!set {alice: null}", ()),
        (
            "- accounts: {github: alice}\n  authorizations: {sab: !!set {}}",
            (0, "authorizations", "sab"),
        ),
        (
            "- accounts: {github: alice}\n  authorizations: {sab: !!set {read: null}}",
            (0, "authorizations", "sab"),
        ),
    ],
)
def test_rejects_yaml_sets_before_sequence_normalization(
    source: str, location: tuple[str | int, ...]
) -> None:
    """YAML sets cannot be interpreted as person or authorization sequences."""
    with pytest.raises(parser.InvalidUsersDocumentError) as caught:
        parser.parse_users_yaml(source)
    assert caught.value.errors[0]["type"] == "sequence_required"
    assert caught.value.errors[0]["location"] == location
