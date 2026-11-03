"""Render public/private repository modes and reject credential URL leakage."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import validate
from prepare import repository_credentials


class RepositoryCredentialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.target = validate.offline_target_config()
        self.target["repositoryCredentials"] = {
            "enabled": True, "urlPrefix": "https://github.com/hoi3dkhoiche-byte/"}

    def render(self, config):
        path = self.directory / "target.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return validate.render(ROOT / "argocd-secrets", [path])

    def test_public_mode_keeps_only_admin_and_redis_external_secrets(self):
        self.target["repositoryCredentials"]["enabled"] = False
        documents = self.render(self.target)
        validate.validate_repository_secrets(documents, self.target)
        self.assertEqual({item["metadata"]["name"] for item in documents if item["kind"] == "ExternalSecret"},
                         {"argocd-admin", "argocd-redis"})

    def test_private_mode_renders_one_shared_eso_template_for_both_repositories(self):
        documents = self.render(self.target)
        validate.validate_repository_secrets(documents, self.target)
        external = next(item for item in documents if item["metadata"]["name"] == "argocd-repositories")
        target = external["spec"]["target"]
        self.assertEqual(target["name"], "argocd-github-credentials")
        prefix = target["template"]["data"]["url"]
        self.assertTrue(all(self.target[name].startswith(prefix) for name in ("sourceRepo", "gitopsRepo")))
        self.assertFalse(any(item["kind"] == "Secret" for item in documents), "Git must not render an inline credential Secret")

    def test_private_credentials_do_not_bypass_aws_activation_guard(self):
        self.target["deployment"]["enabled"] = False
        self.target["accountId"] = "REPLACE_FROM_TERRAFORM"
        self.assertEqual(self.render(self.target), [])

    def test_prefix_must_include_owner_boundary_and_cover_both_repositories(self):
        for prefix in ("https://github.com/hoi3dkhoiche-byte", "https://github.com/",
                       "https://github.com/hoi3dkhoiche-byte-evil/",
                       self.target["gitopsRepo"]):
            target = copy.deepcopy(self.target)
            target["repositoryCredentials"]["urlPrefix"] = prefix
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(ValueError, "URL prefix"):
                    self.render(target)
                with self.assertRaisesRegex(ValueError, "URL prefix"):
                    repository_credentials(target)

    def test_uncovered_source_or_gitops_repository_is_rejected(self):
        for name in ("sourceRepo", "gitopsRepo"):
            for value in ("https://github.com/another-owner/repo.git",
                          "https://github.com/hoi3dkhoiche-byte-evil/repo.git",
                          "https://user:token@github.com/hoi3dkhoiche-byte/repo.git",
                          "https://github.com/hoi3dkhoiche-byte/repo.git?token=secret",
                          "git@github.com:hoi3dkhoiche-byte/repo.git"):
                target = copy.deepcopy(self.target)
                target[name] = value
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, "must cover"):
                        self.render(target)
                    with self.assertRaisesRegex(ValueError, "must cover"):
                        repository_credentials(target)

    def test_plaintext_credentials_are_not_accepted_even_when_disabled(self):
        for key in ("username", "password", "token"):
            for enabled in (False, True):
                target = copy.deepcopy(self.target)
                target["repositoryCredentials"].update(enabled=enabled, **{key: "do-not-store-this-in-git"})
                with self.subTest(key=key, enabled=enabled):
                    with self.assertRaisesRegex(ValueError, "metadata only"):
                        repository_credentials(target)
                    with self.assertRaisesRegex(ValueError, "metadata only"):
                        self.render(target)

    def test_enablement_is_a_boolean(self):
        target = copy.deepcopy(self.target)
        target["repositoryCredentials"]["enabled"] = "false"
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            self.render(target)
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            repository_credentials(target)


if __name__ == "__main__":
    unittest.main()
