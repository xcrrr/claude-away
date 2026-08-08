"""The deterministic safety policy.

The question every test here asks: is there any way to end up with more authority than the
configuration granted?
"""

from __future__ import annotations

import pytest

from claude_away.core.policy import (
    Operation,
    PolicyDecision,
    SafetyPolicy,
    normalise_ref,
    normalise_repo_path,
)
from claude_away.errors import PolicyDeniedError, ValidationError
from tests.conftest import config_document

PERMISSIVE = SafetyPolicy(
    allow_commit=True,
    allow_push=True,
    allow_merge=True,
    allow_deploy=True,
    allow_destructive=True,
)
DEFAULT = SafetyPolicy()


class TestTableIntegrity:
    def test_every_operation_has_a_rule(self) -> None:
        """A new operation without a rule must be caught here, not default to allow."""
        for operation in Operation:
            decision = DEFAULT.evaluate(operation)
            assert isinstance(decision, PolicyDecision)
            assert decision.rule != "unclassified_operation", (
                f"{operation.value} has no policy rule"
            )

    def test_an_unclassified_operation_is_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulate the table losing an entry: the answer must be deny, not crash-open."""
        from claude_away.core import policy as policy_module

        table = dict(policy_module._RULES)
        table.pop(Operation.COMMIT)
        monkeypatch.setattr(policy_module, "_RULES", table)

        decision = PERMISSIVE.evaluate(Operation.COMMIT)
        assert not decision.allowed
        assert decision.rule == "unclassified_operation"

    def test_decisions_are_explainable(self) -> None:
        decision = DEFAULT.evaluate(Operation.PUSH)
        payload = decision.to_dict()
        assert payload["operation"] == "push"
        assert payload["allowed"] is False
        assert payload["rule"] == "allow_push"
        assert "allowPush" in payload["reason"]


class TestDefaultsDeny:
    @pytest.mark.parametrize(
        "operation",
        [
            Operation.PUSH,
            Operation.FORCE_PUSH,
            Operation.MERGE,
            Operation.DEPLOY,
            Operation.DESTRUCTIVE,
            Operation.DELETE_BRANCH,
            Operation.REWRITE_HISTORY,
            Operation.OPEN_PULL_REQUEST,
        ],
    )
    def test_denied_under_the_shipped_defaults(self, operation: Operation) -> None:
        policy = SafetyPolicy.from_config(config_document())
        assert not policy.evaluate(operation).allowed

    def test_inspection_is_always_allowed(self) -> None:
        assert SafetyPolicy(allow_commit=False).evaluate(Operation.INSPECT).allowed
        assert SafetyPolicy(allow_commit=False).evaluate(Operation.READ_FILE).allowed

    def test_from_config_defaults_to_deny_when_a_key_is_absent(self) -> None:
        """An absent flag means no, never yes."""
        policy = SafetyPolicy.from_config({"safety": {}})
        assert not policy.allow_commit
        assert not policy.allow_push
        assert not policy.evaluate(Operation.COMMIT).allowed


class TestNoInferredAuthority:
    def test_allow_push_does_not_grant_force_push(self) -> None:
        """The one that matters most: force push destroys other people's history."""
        decision = PERMISSIVE.evaluate(Operation.FORCE_PUSH)
        assert not decision.allowed
        assert decision.rule == "force_push_never_permitted"

    def test_allow_destructive_does_not_grant_force_push(self) -> None:
        assert not PERMISSIVE.evaluate(Operation.FORCE_PUSH).allowed

    def test_nothing_grants_history_rewriting(self) -> None:
        assert not PERMISSIVE.evaluate(Operation.REWRITE_HISTORY).allowed

    def test_allow_commit_does_not_grant_push(self) -> None:
        policy = SafetyPolicy(allow_commit=True)
        assert policy.evaluate(Operation.COMMIT).allowed
        assert not policy.evaluate(Operation.PUSH).allowed

    def test_allow_push_does_not_grant_merge(self) -> None:
        policy = SafetyPolicy(allow_push=True)
        assert policy.evaluate(Operation.PUSH).allowed
        assert not policy.evaluate(Operation.MERGE).allowed

    def test_allow_merge_does_not_grant_deploy(self) -> None:
        policy = SafetyPolicy(allow_merge=True)
        assert policy.evaluate(Operation.MERGE).allowed
        assert not policy.evaluate(Operation.DEPLOY).allowed

    def test_allow_commit_does_not_grant_branch_deletion(self) -> None:
        policy = SafetyPolicy(allow_commit=True)
        assert not policy.evaluate(Operation.DELETE_BRANCH).allowed

    def test_local_mutation_requires_allow_commit(self) -> None:
        """Producing changes that could never be committed is work we may not finish."""
        policy = SafetyPolicy(allow_commit=False, allow_push=True)
        assert not policy.evaluate(Operation.CREATE_BRANCH).allowed
        assert not policy.evaluate(Operation.WRITE_FILE).allowed
        assert not policy.evaluate(Operation.CREATE_WORKTREE).allowed


class TestProtectedBranches:
    def test_mutating_a_protected_branch_is_denied_despite_every_flag(self) -> None:
        policy = SafetyPolicy(
            allow_commit=True,
            allow_push=True,
            allow_merge=True,
            allow_destructive=True,
            protected_branches=("main",),
        )
        for operation in (
            Operation.COMMIT,
            Operation.PUSH,
            Operation.MERGE,
            Operation.DELETE_BRANCH,
        ):
            decision = policy.evaluate(operation, branch="main")
            assert not decision.allowed, operation
            assert decision.rule == "protected_branch"

    def test_a_feature_branch_is_unaffected(self) -> None:
        policy = SafetyPolicy(allow_commit=True, protected_branches=("main",))
        assert policy.evaluate(Operation.COMMIT, branch="claude-away/AWAY-0001").allowed

    def test_reading_a_protected_branch_is_fine(self) -> None:
        policy = SafetyPolicy(protected_branches=("main",))
        assert policy.evaluate(Operation.INSPECT, branch="main").allowed

    def test_branch_matching_is_exact(self) -> None:
        """`main` must not protect `maintenance` by accident, nor miss `main`."""
        policy = SafetyPolicy(allow_commit=True, protected_branches=("main",))
        assert policy.evaluate(Operation.COMMIT, branch="maintenance").allowed
        assert not policy.evaluate(Operation.COMMIT, branch="main").allowed


class TestProtectedPaths:
    POLICY = SafetyPolicy(
        allow_commit=True,
        allow_push=True,
        allow_destructive=True,
        protected_paths=("infra", ".github/workflows", "secrets/prod.env"),
    )

    @pytest.mark.parametrize(
        "path",
        [
            "infra",
            "infra/main.tf",
            "infra/nested/deep/file.txt",
            ".github/workflows",
            ".github/workflows/ci.yml",
            "secrets/prod.env",
            "./infra/main.tf",
            "/infra/main.tf",
        ],
    )
    def test_protected_paths_match(self, path: str) -> None:
        assert self.POLICY.is_path_protected(path), path

    @pytest.mark.parametrize(
        "path",
        [
            "infrastructure.md",
            "infra-notes.txt",
            "src/infra.py",
            ".github/dependabot.yml",
            "secrets/prod.env.example",
            "README.md",
        ],
    )
    def test_unprotected_paths_do_not_match(self, path: str) -> None:
        """Component-wise, not raw prefix: `infra` must not swallow `infrastructure.md`."""
        assert not self.POLICY.is_path_protected(path), path

    def test_backslash_separators_do_not_evade(self) -> None:
        """A Windows-style path must not slip past a POSIX-written guard."""
        assert self.POLICY.is_path_protected("infra\\main.tf")
        assert self.POLICY.is_path_protected(".github\\workflows\\ci.yml")

    def test_dot_segments_are_refused_rather_than_normalised_away(self) -> None:
        """`..` in a repo-relative path is meaningless and is treated as protected."""
        assert self.POLICY.is_path_protected("../../etc/passwd")
        assert self.POLICY.is_path_protected("infra/../src/app.py")
        with pytest.raises(ValueError, match=r"\.\."):
            normalise_repo_path("a/../b")

    def test_mutation_touching_a_protected_path_is_denied(self) -> None:
        decision = self.POLICY.evaluate(Operation.COMMIT, paths=["src/app.py", "infra/main.tf"])
        assert not decision.allowed
        assert decision.rule == "protected_paths"
        assert decision.detail == {"paths": ["infra/main.tf"]}

    def test_mutation_avoiding_protected_paths_is_allowed(self) -> None:
        assert self.POLICY.evaluate(Operation.COMMIT, paths=["src/app.py"]).allowed

    def test_reading_a_protected_path_is_allowed(self) -> None:
        assert self.POLICY.evaluate(Operation.READ_FILE, paths=["infra/main.tf"]).allowed

    def test_protected_paths_beat_every_permission_flag(self) -> None:
        permissive = SafetyPolicy(
            allow_commit=True,
            allow_push=True,
            allow_merge=True,
            allow_deploy=True,
            allow_destructive=True,
            protected_paths=("infra",),
        )
        assert not permissive.evaluate(Operation.DESTRUCTIVE, paths=["infra/x"]).allowed


class TestPathNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("src/app.py", "src/app.py"),
            ("./src/app.py", "src/app.py"),
            ("src\\app.py", "src/app.py"),
            ("  src/app.py  ", "src/app.py"),
            ("././src/app.py", "src/app.py"),
        ],
    )
    def test_normalisation(self, raw: str, expected: str) -> None:
        assert str(normalise_repo_path(raw)) == expected

    def test_parent_segments_are_rejected(self) -> None:
        for raw in ["../x", "a/../b", "..", "a/b/../../.."]:
            with pytest.raises(ValueError):
                normalise_repo_path(raw)

    @pytest.mark.parametrize(
        "raw", ["/src/app.py", "/home/user/repo/infra/deploy.tf", "\\windows\\path"]
    )
    def test_absolute_paths_are_refused_not_reinterpreted(self, raw: str) -> None:
        """Stripping the leading slash silently produced a wrong answer.

        `/home/user/repo/infra/deploy.tf` became `home/user/repo/infra/deploy.tf`, which
        shares no component with the entry `infra`, so an obviously protected file
        evaluated as unprotected. Absolute paths are the natural shape of input from a
        tool-use hook, which makes it the fail-open that would have mattered.
        """
        with pytest.raises(ValueError, match="repository-relative"):
            normalise_repo_path(raw)

    def test_an_absolute_path_still_fails_closed_at_the_policy(self) -> None:
        policy = SafetyPolicy(allow_commit=True, protected_paths=("infra",))
        assert policy.is_path_protected("/home/user/repo/infra/deploy.tf")
        assert policy.is_path_protected("/anything/at/all")
        assert not policy.evaluate(Operation.COMMIT, paths=["/home/user/repo/src/app.py"]).allowed


class TestEnforcement:
    def test_raise_if_denied(self) -> None:
        with pytest.raises(PolicyDeniedError) as caught:
            DEFAULT.evaluate(Operation.PUSH).raise_if_denied()
        assert caught.value.operation == "push"
        assert caught.value.rule == "allow_push"

    def test_raise_if_denied_is_silent_when_allowed(self) -> None:
        PERMISSIVE.evaluate(Operation.COMMIT).raise_if_denied()

    def test_denial_detail_reaches_the_error(self) -> None:
        policy = SafetyPolicy(allow_commit=True, protected_paths=("infra",))
        with pytest.raises(PolicyDeniedError) as caught:
            policy.evaluate(Operation.COMMIT, paths=["infra/main.tf"]).raise_if_denied()
        assert caught.value.details["detail"] == {"paths": ["infra/main.tf"]}

    @pytest.mark.parametrize("colliding_key", ["operation", "rule", "reason", "message"])
    def test_detail_never_collides_with_the_errors_own_fields(self, colliding_key: str) -> None:
        """`detail` is carried as one nested value, not splatted into the constructor.

        Splatting made the raise itself depend on which keys a rule happened to put in
        `detail`: a rule adding `detail={"reason": ...}` turned a clean denial into
        `TypeError: got multiple values for keyword argument 'reason'`, which no
        `except PolicyDeniedError` handler catches. No shipped rule produces such a key
        today, so this guards the next rule someone writes rather than a live crash.
        """
        decision = PolicyDecision(
            Operation.PUSH,
            False,
            rule="allow_push",
            reason="denied",
            detail={colliding_key: "value-from-detail"},
        )
        with pytest.raises(PolicyDeniedError) as caught:
            decision.raise_if_denied()

        assert caught.value.operation == "push"
        assert caught.value.rule == "allow_push"
        assert caught.value.details["detail"] == {colliding_key: "value-from-detail"}

    def test_policy_evaluation_is_pure(self) -> None:
        """Same inputs, same answer -- no hidden state, no I/O, no model input."""
        first = PERMISSIVE.evaluate(Operation.COMMIT, branch="x", paths=["a"])
        second = PERMISSIVE.evaluate(Operation.COMMIT, branch="x", paths=["a"])
        assert first == second


class TestFromConfig:
    def test_reads_the_shipped_config_contract(self) -> None:
        document = config_document()
        document["safety"] = {
            "allowCommit": True,
            "allowPush": True,
            "allowMerge": False,
            "allowDeploy": False,
            "allowDestructive": False,
            "protectedPaths": ["infra/"],
        }
        policy = SafetyPolicy.from_config(document, protected_branches=["main"])
        assert policy.allow_commit and policy.allow_push
        assert not policy.allow_merge
        assert policy.is_path_protected("infra/main.tf")
        assert policy.is_branch_protected("main")

    def test_trailing_slash_in_a_protected_path_still_matches(self) -> None:
        policy = SafetyPolicy(allow_commit=True, protected_paths=("infra/",))
        assert policy.is_path_protected("infra/main.tf")
        assert policy.is_path_protected("infra")

    def test_empty_protected_branches_are_ignored(self) -> None:
        policy = SafetyPolicy.from_config(config_document(), protected_branches=["", None])  # type: ignore[list-item]
        assert policy.protected_branches == ()
        assert not policy.is_branch_protected(None)


class TestConfigurationIsTypeCheckedNotCoerced:
    """`bool("false")` is True. That is the worst possible way to read a safety flag."""

    @pytest.mark.parametrize("value", ["false", "no", "0", 0, 1, "true", []])
    def test_a_non_boolean_flag_is_refused(self, value: object) -> None:
        with pytest.raises(ValidationError, match="must be true or false"):
            SafetyPolicy.from_config({"safety": {"allowPush": value}})

    def test_real_booleans_are_accepted(self) -> None:
        policy = SafetyPolicy.from_config({"safety": {"allowPush": True, "allowCommit": False}})
        assert policy.allow_push and not policy.allow_commit

    def test_a_string_protected_paths_is_refused_rather_than_iterated(self) -> None:
        """`tuple("infra")` is ('i','n','f','r','a'), which protects nothing at all."""
        with pytest.raises(ValidationError, match="list of strings"):
            SafetyPolicy.from_config({"safety": {"protectedPaths": "infra"}})

    def test_a_null_safety_block_is_a_domain_error_not_an_attribute_error(self) -> None:
        policy = SafetyPolicy.from_config({"safety": None})
        assert not policy.allow_commit

    def test_a_non_object_safety_block_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must be an object"):
            SafetyPolicy.from_config({"safety": ["allowPush"]})


class TestProtectedPathsThatCannotBeHonoured:
    """An entry that protects nothing must be a configuration error, not a silent skip."""

    @pytest.mark.parametrize("entry", ["../secrets", "deploy/..", "..", "a/../b"])
    def test_dot_segment_entries_are_refused(self, entry: str) -> None:
        with pytest.raises(ValidationError, match="cannot be honoured"):
            SafetyPolicy.from_config({"safety": {"protectedPaths": [entry]}})

    @pytest.mark.parametrize("entry", ["infra/**", "infra/*", "secrets/*.env", "log[0-9]"])
    def test_glob_entries_are_refused(self, entry: str) -> None:
        """Matching is component-wise, so a glob entry equals no real path.

        Refusing is the point: `awayctl policy` printed these verbatim under "protected
        paths", which is positive confirmation of protection that did not exist.
        """
        with pytest.raises(ValidationError, match="glob"):
            SafetyPolicy.from_config({"safety": {"protectedPaths": [entry]}})

    def test_an_absolute_entry_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="cannot be honoured"):
            SafetyPolicy.from_config({"safety": {"protectedPaths": ["/etc/passwd"]}})

    def test_ordinary_entries_are_accepted(self) -> None:
        policy = SafetyPolicy.from_config(
            {"safety": {"protectedPaths": ["infra", ".github/workflows", "secrets/prod.env"]}}
        )
        assert policy.is_path_protected("infra/main.tf")


class TestBranchSpellings:
    """Git yields different spellings depending on which incantation produced the value."""

    @pytest.mark.parametrize(
        "spelling",
        [
            "main",
            "refs/heads/main",
            "heads/main",
            "refs/remotes/origin/main",
            "remotes/origin/main",
        ],
    )
    def test_every_spelling_of_a_protected_branch_is_protected(self, spelling: str) -> None:
        policy = SafetyPolicy(allow_commit=True, protected_branches=("main",))
        assert policy.is_branch_protected(spelling), spelling
        assert not policy.evaluate(Operation.COMMIT, branch=spelling).allowed

    def test_a_protected_entry_written_as_a_full_ref_still_matches_the_short_name(self) -> None:
        policy = SafetyPolicy(allow_commit=True, protected_branches=("refs/heads/main",))
        assert policy.is_branch_protected("main")

    def test_a_different_branch_is_still_unaffected(self) -> None:
        policy = SafetyPolicy(allow_commit=True, protected_branches=("main",))
        assert policy.evaluate(Operation.COMMIT, branch="refs/heads/feature").allowed
        assert not policy.is_branch_protected("maintenance")

    def test_normalise_ref_is_exported_and_stable(self) -> None:
        assert normalise_ref("refs/heads/x") == "x"
        assert normalise_ref("refs/remotes/upstream/x") == "x"
        assert normalise_ref("x") == "x"


class TestTheDefaultIsDenial:
    def test_every_flag_defaults_to_false(self) -> None:
        """allow_commit defaulted to True: the one allow-by-default field in the module.

        `SafetyPolicy(protected_paths=..., protected_branches=...)` is the natural call
        shape for seeding guards, and it used to hand out commit, write, branch and
        worktree authority nobody had configured.
        """
        policy = SafetyPolicy()
        for operation in Operation:
            decision = policy.evaluate(operation)
            if operation in (Operation.INSPECT, Operation.READ_FILE):
                assert decision.allowed, operation
            else:
                assert not decision.allowed, operation

    def test_seeding_only_the_guards_grants_nothing(self) -> None:
        policy = SafetyPolicy(protected_paths=("infra",), protected_branches=("main",))
        for operation in (
            Operation.COMMIT,
            Operation.WRITE_FILE,
            Operation.CREATE_BRANCH,
            Operation.CREATE_WORKTREE,
        ):
            assert not policy.evaluate(operation).allowed, operation
