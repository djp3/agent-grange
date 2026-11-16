"""Is this image current relative to a GitHub tag?

Shared by entrypoint.py (decides start vs refuse at launch) and server.py
(decides healthy vs unhealthy while running). Nothing here prints or exits;
callers act on the returned Result.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum

FIX = "make update   (git pull --ff-only, then make up)"


class Verdict(Enum):
    FRESH = "fresh"                  # tag points at this image's commit
    AHEAD = "ahead"                  # image is newer than the tag (unreleased work)
    STALE = "stale"                  # tag is newer than, or diverged from, this image
    UNVERIFIABLE = "unverifiable"    # tag missing, GitHub unreachable, token problem
    NO_METADATA = "no-metadata"      # image built without the Makefile


@dataclass
class Config:
    repo: str
    built: str
    tag: str
    pushed_bases: list[str]
    dirty: bool
    strict: bool
    skip: bool
    api: str
    token: str | None

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        built = env.get("GIT_COMMIT", "unknown")
        return cls(
            repo=env.get("GIT_REPO", "unknown"),
            built=built,
            tag=env.get("CHECK_TAG", "").strip(),
            pushed_bases=[b for b in env.get("GIT_PUSHED_BASE", "").split() if b and b != built],
            dirty=env.get("GIT_DIRTY") == "1",
            strict=env.get("FRESHNESS_STRICT", "1") == "1",
            skip=env.get("SKIP_FRESHNESS_CHECK") == "1",
            api=env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            token=env.get("GITHUB_TOKEN") or None,
        )


@dataclass
class Result:
    verdict: Verdict
    summary: str                                   # one line: logs, health log, 503 body
    details: list[str] = field(default_factory=list)   # extra lines for banners
    tagged: str | None = None                      # commit the tag resolved to, if known

    def blocks_start(self, strict: bool) -> bool:
        return (self.verdict in (Verdict.STALE, Verdict.NO_METADATA)
                or (self.verdict is Verdict.UNVERIFIABLE and strict))

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "summary": self.summary,
                "details": self.details, "tag_commit": self.tagged}


class GitHub:
    def __init__(self, api: str, token: str | None, repo: str):
        self.api, self.token, self.repo = api, token, repo
        self.tag_ref_etag: str | None = None      # ETag of the last tag-ref response

    def _request(self, path: str, etag: str | None = None):
        """(status, json_or_None, etag). With `etag`, sends If-None-Match and may return 304.

        GitHub does not count a 304 against the rate limit, which is what makes
        frequent polling affordable.
        """
        req = urllib.request.Request(f"{self.api}{path}", headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agentic-grange-freshness-check",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp), resp.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None, etag
            raise

    def get(self, path: str) -> dict:
        return self._request(path)[1]

    def tag_ref(self, tag: str, etag: str | None = None):
        """(changed, ref_json). changed is False on a 304, and ref_json is then None."""
        status, data, new_etag = self._request(f"/repos/{self.repo}/git/ref/tags/{tag}", etag)
        self.tag_ref_etag = new_etag
        return status != 304, data

    def tag_commit(self, tag: str) -> str:
        """Commit SHA a tag points to. Annotated tags are one indirection away."""
        _, ref = self.tag_ref(tag)
        obj = ref["object"]
        if obj["type"] == "tag":
            obj = self.get(f"/repos/{self.repo}/git/tags/{obj['sha']}")["object"]
        return obj["sha"]

    def compare(self, base: str, head: str):
        """(status, head_ahead_by, head_behind_by), or None if GitHub does not know a commit.

        status: identical | ahead (head is newer) | behind (head is older) | diverged.
        """
        try:
            c = self.get(f"/repos/{self.repo}/compare/{base}...{head}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return c["status"], c["ahead_by"], c["behind_by"]


NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError)


def assess(cfg: Config, gh: "GitHub | None" = None) -> Result:
    """One full check. Makes 1-3 GitHub API calls. Never raises for expected failures."""
    short = cfg.built[:12]
    if "unknown" in (cfg.repo, cfg.built):
        return Result(Verdict.NO_METADATA,
                      f"image has no git metadata (repo={cfg.repo} commit={short}); "
                      f"it was built without the Makefile",
                      ["Run `make build` or `make up`."])
    if not cfg.tag:
        return Result(Verdict.NO_METADATA,
                      "CHECK_TAG is empty; set it in docker-compose.yml, e.g. agentic-grange-latest")

    gh = gh or GitHub(cfg.api, cfg.token, cfg.repo)
    try:
        tagged = gh.tag_commit(cfg.tag)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return Result(Verdict.UNVERIFIABLE,
                          f"tag '{cfg.tag}' not found in {cfg.repo}: not created yet, "
                          f"or private repo without GITHUB_TOKEN")
        return Result(Verdict.UNVERIFIABLE, f"GitHub API {e.code} resolving tag '{cfg.tag}' in {cfg.repo}")
    except NETWORK_ERRORS as e:
        return Result(Verdict.UNVERIFIABLE, f"cannot reach {cfg.api}: {e}")

    if tagged == cfg.built:
        return Result(Verdict.FRESH, f"tag '{cfg.tag}' points at {short}, same as this image", tagged=tagged)

    try:
        rel = gh.compare(cfg.built, tagged)           # base = this image, head = the tag
        if rel is None:                               # GitHub has never seen our commit
            return _judge_unpushed(cfg, gh, tagged)
    except urllib.error.HTTPError as e:
        return Result(Verdict.UNVERIFIABLE, f"GitHub compare API {e.code}", tagged=tagged)
    except NETWORK_ERRORS as e:
        return Result(Verdict.UNVERIFIABLE, f"cannot reach {cfg.api}: {e}", tagged=tagged)

    status, newer, older = rel
    if status == "ahead":                             # the tag is ahead of the image
        return Result(Verdict.STALE,
                      f"image is {newer} commit(s) behind tag '{cfg.tag}'",
                      [f"image built from {short}",
                       f"tag '{cfg.tag}' now points at {tagged[:12]}, {newer} commit(s) newer"],
                      tagged)
    if status == "diverged":
        return Result(Verdict.STALE,
                      f"image diverged from tag '{cfg.tag}', which has {newer} commit(s) this image lacks",
                      [f"image built from {short}, tag '{cfg.tag}' is at {tagged[:12]}",
                       f"diverged: the tag has {newer} commit(s) you lack, you have {older} it lacks"],
                      tagged)
    if status == "behind":                            # the image is ahead of the tag
        return Result(Verdict.AHEAD,
                      f"image is {older} commit(s) ahead of tag '{cfg.tag}' (unreleased work)", tagged=tagged)
    return Result(Verdict.FRESH, f"compare status {status}", tagged=tagged)


def _judge_unpushed(cfg: Config, gh: GitHub, tagged: str) -> Result:
    """GitHub has never seen `built`. Decide by its nearest pushed ancestor(s).

    If the tag's commit is an ancestor of a pushed ancestor of `built`, then
    `built` descends from the tag: unreleased work, AHEAD. Otherwise the tag has
    history this image lacks: STALE.
    """
    short = cfg.built[:12]
    if not cfg.pushed_bases:
        return Result(Verdict.UNVERIFIABLE,
                      f"commit {short} is not on GitHub and no pushed ancestor was recorded "
                      f"at build time (GIT_PUSHED_BASE)", tagged=tagged)
    first = None
    try:
        for base in cfg.pushed_bases:
            if base == tagged:
                return Result(Verdict.AHEAD,
                              f"unpushed commit {short} is built directly on tag '{cfg.tag}' ({tagged[:12]})",
                              tagged=tagged)
            rel = gh.compare(base, tagged)
            if rel is None:
                continue                              # this ancestor is unknown to GitHub too
            status, newer, older = rel
            if status in ("behind", "identical"):     # tag is an ancestor of our pushed base
                return Result(Verdict.AHEAD,
                              f"unpushed commit {short} descends from tag '{cfg.tag}' "
                              f"(its pushed ancestor {base[:12]} is {older} commit(s) past the tag)",
                              tagged=tagged)
            first = first or (base, status, newer, older)
    except urllib.error.HTTPError as e:
        return Result(Verdict.UNVERIFIABLE, f"GitHub compare API {e.code}", tagged=tagged)
    except NETWORK_ERRORS as e:
        return Result(Verdict.UNVERIFIABLE, f"cannot reach {cfg.api}: {e}", tagged=tagged)

    if first is None:
        return Result(Verdict.UNVERIFIABLE,
                      f"commit {short} is not on GitHub and neither is any recorded pushed ancestor "
                      f"({', '.join(b[:12] for b in cfg.pushed_bases)})", tagged=tagged)
    base, status, newer, older = first
    if status == "ahead":
        return Result(Verdict.STALE,
                      f"unpushed image is behind tag '{cfg.tag}': the tag is {newer} commit(s) "
                      f"past its pushed ancestor {base[:12]}",
                      [f"image built from unpushed commit {short}, whose pushed ancestor is {base[:12]}",
                       f"tag '{cfg.tag}' points at {tagged[:12]}, {newer} commit(s) past that ancestor"],
                      tagged)
    return Result(Verdict.STALE,
                  f"unpushed image diverged from tag '{cfg.tag}'",
                  [f"image built from unpushed commit {short}, whose pushed ancestor is {base[:12]}",
                   f"diverged from tag '{cfg.tag}' at {tagged[:12]}: the tag has {newer} commit(s) "
                   f"you lack, you have {older} it lacks"],
                  tagged)


DEFINITIVE = (Verdict.FRESH, Verdict.AHEAD, Verdict.STALE)


class Checker:
    """Repeated checks that cost nothing while the tag stands still.

    Remembers the ETag of the tag reference and the last definitive Result.
    Each poll first asks GitHub for the tag ref with If-None-Match:

      304  the tag has not moved, so the previous verdict still holds. No
           further calls, and GitHub does not charge the request.
      200  the tag moved (or this is the first poll): run the full assess().

    UNVERIFIABLE results do not replace the remembered verdict, so a network
    blip followed by a 304 correctly returns the last known good answer.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.etag: str | None = None
        self.last: Result | None = None
        self.unchanged_polls = 0

    def poll(self):
        """(result, changed). changed is False when a 304 confirmed nothing moved."""
        cfg = self.cfg
        gh = GitHub(cfg.api, cfg.token, cfg.repo)
        if self.etag and self.last is not None:
            try:
                changed, _ = gh.tag_ref(cfg.tag, etag=self.etag)
            except urllib.error.HTTPError:
                changed = True                   # e.g. tag deleted: let assess() explain
            except NETWORK_ERRORS as e:
                return Result(Verdict.UNVERIFIABLE, f"cannot reach {cfg.api}: {e}", tagged=self.last.tagged), True
            if not changed:
                self.unchanged_polls += 1
                return self.last, False
        res = assess(cfg, gh)
        if res.verdict in DEFINITIVE:
            self.etag, self.last = gh.tag_ref_etag, res
            self.unchanged_polls = 0
        return res, True
