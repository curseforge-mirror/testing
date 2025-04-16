import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_fixed

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cf-release")

# ─── Constants ───────────────────────────────────────────────────────────────
CF_API = "https://api.curseforge.com/v1"
GH_API = "https://api.github.com"
BIGWIGS_RELEASE_SH = (
    "https://raw.githubusercontent.com/BigWigsMods/packager/master/release.sh"
)
GAME_ID = 1  # WoW
RELEASE_SH_LOCAL = "release.sh"


# ─── Helpers ─────────────────────────────────────────────────────────────────
def env_or_fail(var):
    v = os.getenv(var)
    if not v:
        log.error(f"env var {var} is required")
        sys.exit(1)
    return v


def make_session(cf_token, gh_token=None):
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "x-api-key": cf_token})
    if gh_token:
        s.headers.update({"Authorization": f"Bearer {gh_token}"})
    return s


def html_to_markdown(html: str) -> str:
    """Rudimentary HTML→Markdown converter using BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href", "")
        text = a.get_text().strip()
        a.replace_with(f"[{text}]({href})")
    for lvl in (1, 2, 3):
        for h in soup.find_all(f"h{lvl}"):
            prefix = "#" * lvl + " "
            h.insert_before("\n" + prefix)
            h.append("\n")
    for li in soup.find_all("li"):
        li.insert_before("- ")
        li.append("\n")
    for p in soup.find_all("p"):
        p.insert_before("\n")
        p.append("\n")
    return soup.get_text()


class FunctionExtractionError(Exception):
    pass


# ─── Pipeline ────────────────────────────────────────────────────────────────
class ReleasePipeline:
    def __init__(self, mod_id, cf_token, gh_token):
        self.mod_id = int(mod_id)
        self.cf = make_session(cf_token)
        self.gh = make_session(cf_token, gh_token)
        self.version_types = self._load_version_types()
        self._fetch_and_save_release_sh()
        self.mod_name = self._fetch_mod_name()

    def _load_version_types(self):
        r = self.cf.get(f"{CF_API}/games/{GAME_ID}/version-types")
        r.raise_for_status()
        return {d["id"]: d["slug"] for d in r.json()["data"]}

    def _fetch_and_save_release_sh(self):
        # If we already have it and it’s less than 7 days old, reuse it:
        if os.path.exists(RELEASE_SH_LOCAL):
            age = time.time() - os.path.getmtime(RELEASE_SH_LOCAL)
            if age < 7 * 24 * 3600:
                log.info("→ Using cached release.sh")
                return

        log.info("→ Downloading BigWigs release.sh")
        r = requests.get(BIGWIGS_RELEASE_SH)
        r.raise_for_status()
        with open(RELEASE_SH_LOCAL, "w") as f:
            f.write(r.text)
        os.chmod(RELEASE_SH_LOCAL, 0o755)

    def _bash_toc_to_type(self, interface: int) -> str:
        """Call the toc_to_type function in release.sh and return the game_type."""

        with open(RELEASE_SH_LOCAL, "r") as f:
            lines = f.readlines()

        func_body = []
        capture = False
        brace_count = 0
        for line in lines:
            if not capture:
                if re.match(r"^\s*toc_to_type\s*\(\)\s*\{", line):
                    capture = True
                    brace_count += 1
                    func_body.append(line)
            else:
                brace_count += line.count("{")
                brace_count -= line.count("}")
                func_body.append(line)
                if brace_count == 0:
                    break

        if brace_count != 0:
            raise FunctionExtractionError(
                "Failed to extract toc_to_type: mismatched braces"
            )

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as temp_script:
            temp_script.write("#!/usr/bin/env bash\n")
            temp_script.writelines(func_body)
            temp_script.write(f'toc_to_type "{interface}" result\n')
            temp_script.write('echo "$result"\n')
            script_path = temp_script.name

        os.chmod(script_path, 0o755)

        try:
            result = subprocess.run(
                ["/opt/homebrew/bin/bash", script_path],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        finally:
            os.unlink(script_path)

    def toc_to_type(self, toc_version: str | int) -> str:
        """
        Map a numeric TOC interface string (e.g. "11506", "40402") to the game-flavor slug.
        """
        toc_version = str(toc_version)
        # first three digits determine flavor
        if toc_version.startswith("11") and len(toc_version) == 5:
            return "classic"
        if toc_version.startswith("20"):
            return "bcc"
        if toc_version.startswith("30"):
            return "wrath"
        if toc_version.startswith("40"):
            return "cata"
        return "retail"

    def _fetch_mod_name(self):
        r = self.cf.get(f"{CF_API}/mods/{self.mod_id}")
        r.raise_for_status()
        return r.json()["data"]["name"]

    def _get_latest_files(self):
        """Fetch only the latest files for each flavor via /mods/{mod_id}."""
        r = self.cf.get(f"{CF_API}/mods/{self.mod_id}")
        r.raise_for_status()
        data = r.json()["data"]
        latest = {
            f["id"]: f for f in data.get("latestFiles", []) if f["releaseType"] == 1
        }
        seen = set()
        ordered = []
        for idx in data.get("latestFilesIndexes", []):
            fid = idx["fileId"]
            if fid not in seen and fid in latest:
                seen.add(fid)
                ordered.append(latest[fid])
        return ordered

    def _pick_slug(self, info):
        """Compute interface ints, call bash toc_to_type, and pick slug."""
        ivals = []
        fallback = False
        for gv in info["sortableGameVersions"]:
            parts = gv["gameVersionName"].split(".")
            major, minor = int(parts[0]), int(parts[1])
            patch = int(parts[2]) if len(parts) > 2 else 0
            ivals.append(major * 10000 + minor * 100 + patch)
        try:
            slugs = {self._bash_toc_to_type(iv) for iv in ivals}
        except FileNotFoundError:
            fallback = True
            slugs = {self.toc_to_type(iv) for iv in ivals}
        if "retail" in slugs:
            return ""
        if len(slugs) == 1:
            return slugs.pop()

        if fallback:
            return self.toc_to_type(max(ivals))

        return self._bash_toc_to_type(max(ivals))

    def _download_file(self, info):
        slug = self._pick_slug(info)
        base = info["fileName"].rsplit(".zip", 1)[0]
        if slug and not base.endswith(f"-{slug}"):
            base = f"{base}-{slug}"
        fn = f"{base}.zip"
        log.info(f"↓ Downloading {fn}")
        r = self.cf.get(info["downloadUrl"])
        r.raise_for_status()
        with open(fn, "wb") as f:
            f.write(r.content)
        return fn, slug, info

    def _fetch_changelog_md(self, file_id):
        r = self.cf.get(f"{CF_API}/mods/{self.mod_id}/files/{file_id}/changelog")
        r.raise_for_status()
        html = r.json().get("data", "")
        return html_to_markdown(html)

    def _build_manifest(self, downloads):
        m = {"releases": []}
        for fn, slug, info in downloads:
            ver_m = re.search(r"(\d+\.\d+\.\d+)", fn)
            version = ver_m.group(1) if ver_m else ""
            md = []
            for gv in info["sortableGameVersions"]:
                t = gv["gameVersionTypeId"]
                fv = self.version_types.get(t, "mainline")
                parts = list(map(int, gv["gameVersionName"].split(".")))
                iface = (
                    parts[0] * 10000
                    + parts[1] * 100
                    + (parts[2] if len(parts) > 2 else 0)
                )
                md.append({"flavor": fv, "interface": iface})
            m["releases"].append(
                {
                    "name": self.mod_name,
                    "version": version,
                    "filename": fn,
                    "nolib": False,
                    "metadata": md,
                }
            )
        return m

    def _get_or_create_release(self, tag, body_md):
        repo = os.getenv("GITHUB_REPOSITORY", "")
        owner, name = repo.split("/", 1)
        r = self.gh.get(f"{GH_API}/repos/{owner}/{name}/releases/tags/{tag}")
        if r.status_code == 404:
            payload = {
                "tag_name": tag,
                "name": tag,
                "body": body_md,
                "draft": False,
                "prerelease": False,
            }
            r = self.gh.post(f"{GH_API}/repos/{owner}/{name}/releases", json=payload)
        r.raise_for_status()
        return r.json()["upload_url"].split("{")[0]

    def _upload_asset(self, upload_url, fn, content_type):
        log.info(f"↥ Uploading {fn}")
        params = {"name": os.path.basename(fn)}
        headers = {"Content-Type": content_type}
        with open(fn, "rb") as f:
            r = self.gh.post(upload_url, params=params, headers=headers, data=f)
        r.raise_for_status()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
    )
    def run(self):
        log.info(f"→ Starting release for {self.mod_name} (ID {self.mod_id})")

        files = self._get_latest_files()
        if not files:
            log.error("No latest files found on CurseForge.")
            sys.exit(1)

        downloads = [self._download_file(f) for f in files]
        manifest = self._build_manifest(downloads)
        with open("release.json", "w") as f:
            json.dump(manifest, f, indent=2)

        changelogs = [self._fetch_changelog_md(info["id"]) for _, _, info in downloads]
        body_md = "\n\n---\n\n".join(changelogs)

        tag = "v" + datetime.now(timezone.utc).strftime("%Y.%m.%d.%H.%M")
        upload_url = self._get_or_create_release(tag, body_md)

        for fn, _, _ in downloads:
            self._upload_asset(upload_url, fn, "application/zip")
        self._upload_asset(upload_url, "release.json", "application/json")

        log.info("✅ Release complete.")


# ─── Entrypoint ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ADDON_ID = env_or_fail("ADDON_ID")
    CF_API_TOKEN = env_or_fail("CF_API_TOKEN")
    GH_TOKEN = os.getenv("GH_TOKEN", "")
    ReleasePipeline(ADDON_ID, CF_API_TOKEN, GH_TOKEN).run()
