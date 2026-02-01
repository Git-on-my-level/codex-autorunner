from __future__ import annotations

from typing import Optional

import yaml

from .git_mirror import FetchedTemplate
from .scan_cache import TemplateScanRecord


def inject_provenance(
    content: str,
    fetched: FetchedTemplate,
    scan_record: Optional[TemplateScanRecord],
) -> str:
    """Inject deterministic provenance keys into ticket frontmatter.

    Args:
        content: Ticket markdown content
        fetched: The fetched template metadata
        scan_record: Optional scan record for the template

    Returns:
        Updated markdown content with provenance keys in frontmatter

    Notes:
        - Does not embed scan evidence
        - Does not add timestamps (avoids nondeterminism)
        - Creates frontmatter if missing
        - Merges with existing frontmatter without clobbering unrelated keys
    """
    from ...tickets.frontmatter import split_markdown_frontmatter

    fm_yaml, body = split_markdown_frontmatter(content)

    data = {}
    if fm_yaml is not None:
        try:
            parsed = yaml.safe_load(fm_yaml)
            if isinstance(parsed, dict):
                data = parsed
        except yaml.YAMLError:
            pass

    data["template"] = f"{fetched.repo_id}:{fetched.path}@{fetched.ref}"
    data["template_commit"] = fetched.commit_sha
    data["template_blob"] = fetched.blob_sha
    data["template_trusted"] = fetched.trusted

    if scan_record is not None:
        data["template_scan"] = scan_record.decision
    else:
        data["template_scan"] = "skipped"

    rendered = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{rendered}\n---\n{body}"
