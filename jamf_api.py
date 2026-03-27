from __future__ import annotations

"""
Jamf Pro API client.

Classic API (/JSSResource/) always communicates in XML — we send XML
and parse XML back. The Jamf Pro API (/api/v1/) uses JSON for auth
and icon upload only.
"""

import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

INSTALLOMATOR_SCRIPT_NAME = "Installomator"
INSTALLOMATOR_GITHUB_URL = (
    "https://raw.githubusercontent.com/Installomator/Installomator/main/Installomator.sh"
)

_XML_HEADERS = {"Content-Type": "text/xml", "Accept": "text/xml"}


class JamfAPIError(Exception):
    pass


class JamfClient:
    def __init__(self, jamf_url: str, client_id: str, client_secret: str):
        self.base_url = jamf_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Authentication  (Jamf Pro API — JSON)
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        resp = self.session.post(
            f"{self.base_url}/api/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise JamfAPIError(f"Authentication failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 1800) - 30
        self.session.headers.update({"Authorization": f"Bearer {self._token}"})

    def _ensure_token(self) -> None:
        if not self._token or time.time() >= self._token_expiry:
            self.authenticate()

    # ------------------------------------------------------------------
    # Installomator script  (Classic API — XML)
    # ------------------------------------------------------------------

    def get_installomator_script_id(self) -> int | None:
        self._ensure_token()
        resp = self.session.get(
            f"{self.base_url}/JSSResource/scripts",
            headers={"Accept": "text/xml"},
            timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        for script in root.findall("script"):
            name_el = script.find("name")
            id_el   = script.find("id")
            if name_el is not None and name_el.text and \
               name_el.text.lower() == INSTALLOMATOR_SCRIPT_NAME.lower():
                return int(id_el.text)
        return None

    def upload_installomator_script(self, script_contents: str) -> int:
        self._ensure_token()
        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<script>
  <name>{INSTALLOMATOR_SCRIPT_NAME}</name>
  <script_contents><![CDATA[{script_contents}]]></script_contents>
  <parameters>
    <parameter4>Label (e.g. googlechrome)</parameter4>
    <parameter5>NOTIFY= (all/success/silent)</parameter5>
    <parameter6>BLOCKING_PROCESS_ACTION=</parameter6>
    <parameter7>REOPEN= (yes/no)</parameter7>
    <parameter8>IGNORE_APP_STORE_APPS= (yes/no)</parameter8>
    <parameter9>INSTALL= (force or leave blank)</parameter9>
  </parameters>
</script>"""
        resp = self.session.post(
            f"{self.base_url}/JSSResource/scripts/id/0",
            headers=_XML_HEADERS,
            data=payload.encode("utf-8"),
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            raise JamfAPIError(f"Script upload failed ({resp.status_code}): {resp.text}")
        return _parse_created_id(resp.text)

    def ensure_installomator_script(self, script_contents: str | None = None) -> int:
        """
        Return the Installomator script ID, uploading it if needed.
        If script_contents is provided, use that. Otherwise fetch from GitHub.
        """
        script_id = self.get_installomator_script_id()
        if script_id is not None:
            return script_id
        if script_contents is None:
            resp = requests.get(INSTALLOMATOR_GITHUB_URL, timeout=60)
            resp.raise_for_status()
            script_contents = resp.text
        return self.upload_installomator_script(script_contents)

    # ------------------------------------------------------------------
    # Smart Groups  (Classic API — XML)
    # ------------------------------------------------------------------

    def get_computer_group_id(self, name: str) -> int | None:
        self._ensure_token()
        resp = self.session.get(
            f"{self.base_url}/JSSResource/computergroups/name/{requests.utils.quote(name)}",
            headers={"Accept": "text/xml"},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _parse_id_at(resp.text, "id")

    def create_smart_group(self, display_name: str, app_bundle_name: str) -> tuple[int, bool]:
        """
        display_name    : human-readable name, used in the group name  e.g. "Google Chrome"
        app_bundle_name : actual .app bundle stem from the fragment     e.g. "Google Chrome"
                          (we append .app here — do NOT pass it pre-suffixed)
        """
        group_name = f"{display_name} Installed"
        existing = self.get_computer_group_id(group_name)
        if existing is not None:
            return existing, False

        self._ensure_token()
        app_bundle = f"{app_bundle_name}.app"
        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<computer_group>
  <name>{_xml_escape(group_name)}</name>
  <is_smart>true</is_smart>
  <criteria>
    <criterion>
      <name>Application Title</name>
      <priority>0</priority>
      <and_or>and</and_or>
      <search_type>is</search_type>
      <value>{_xml_escape(app_bundle)}</value>
    </criterion>
  </criteria>
</computer_group>"""
        resp = self.session.post(
            f"{self.base_url}/JSSResource/computergroups/id/0",
            headers=_XML_HEADERS,
            data=payload.encode("utf-8"),
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise JamfAPIError(f"Smart Group creation failed ({resp.status_code}): {resp.text}")
        return _parse_created_id(resp.text), True

    # ------------------------------------------------------------------
    # Policies  (Classic API — XML)
    # ------------------------------------------------------------------

    def get_policy_id(self, name: str) -> int | None:
        self._ensure_token()
        resp = self.session.get(
            f"{self.base_url}/JSSResource/policies/name/{requests.utils.quote(name)}",
            headers={"Accept": "text/xml"},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        # Full policy GET: <policy><general><id>
        return _parse_id_at(resp.text, "general", "id")

    def create_self_service_policy(
        self,
        app_name: str,
        label: str,
        script_id: int,
        behavior: dict,
    ) -> tuple[int, bool]:
        policy_name = f"Install {app_name}"
        existing = self.get_policy_id(policy_name)
        if existing is not None:
            return existing, False

        self._ensure_token()
        params_xml = _build_script_params_xml(label, behavior)
        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<policy>
  <general>
    <name>{_xml_escape(policy_name)}</name>
    <enabled>true</enabled>
    <trigger>EVENT</trigger>
    <trigger_other/>
    <frequency>Ongoing</frequency>
    <category><name>No category assigned</name></category>
  </general>
  <scope>
    <all_computers>true</all_computers>
  </scope>
  <self_service>
    <use_for_self_service>true</use_for_self_service>
    <self_service_display_name>{_xml_escape(policy_name)}</self_service_display_name>
    <install_button_text>Install</install_button_text>
    <reinstall_button_text>Reinstall</reinstall_button_text>
    <self_service_description>Install {_xml_escape(app_name)} via Installomator</self_service_description>
    <force_users_to_view_description>false</force_users_to_view_description>
    <notification>false</notification>
  </self_service>
  <scripts>
    <size>1</size>
    <script>
      <id>{script_id}</id>
      <name>{INSTALLOMATOR_SCRIPT_NAME}</name>
      <priority>After</priority>
      {params_xml}
    </script>
  </scripts>
</policy>"""
        resp = self.session.post(
            f"{self.base_url}/JSSResource/policies/id/0",
            headers=_XML_HEADERS,
            data=payload.encode("utf-8"),
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise JamfAPIError(f"Self Service policy creation failed ({resp.status_code}): {resp.text}")
        return _parse_created_id(resp.text), True

    def create_autoupdate_policy(
        self,
        app_name: str,
        label: str,
        script_id: int,
        smart_group_id: int,
        behavior: dict,
    ) -> tuple[int, bool]:
        policy_name = f"Auto-Update {app_name}"
        existing = self.get_policy_id(policy_name)
        if existing is not None:
            return existing, False

        self._ensure_token()
        params_xml = _build_script_params_xml(label, behavior)
        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<policy>
  <general>
    <name>{_xml_escape(policy_name)}</name>
    <enabled>true</enabled>
    <trigger>RECURRING_CHECK_IN</trigger>
    <frequency>Once every day</frequency>
    <category><name>No category assigned</name></category>
  </general>
  <scope>
    <all_computers>false</all_computers>
    <computer_groups>
      <computer_group>
        <id>{smart_group_id}</id>
        <name>{_xml_escape(app_name)} Installed</name>
      </computer_group>
    </computer_groups>
  </scope>
  <self_service>
    <use_for_self_service>false</use_for_self_service>
  </self_service>
  <scripts>
    <size>1</size>
    <script>
      <id>{script_id}</id>
      <name>{INSTALLOMATOR_SCRIPT_NAME}</name>
      <priority>After</priority>
      {params_xml}
    </script>
  </scripts>
</policy>"""
        resp = self.session.post(
            f"{self.base_url}/JSSResource/policies/id/0",
            headers=_XML_HEADERS,
            data=payload.encode("utf-8"),
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise JamfAPIError(f"Auto-Update policy creation failed ({resp.status_code}): {resp.text}")
        return _parse_created_id(resp.text), True

    # ------------------------------------------------------------------
    # Icons  (Jamf Pro API v1 — JSON)
    # ------------------------------------------------------------------

    def upload_icon(self, icon_path: str) -> int | None:
        self._ensure_token()
        try:
            with open(icon_path, "rb") as f:
                resp = self.session.post(
                    f"{self.base_url}/api/v1/icon",
                    files={"file": (Path(icon_path).name, f, "image/png")},
                    timeout=30,
                )
            if resp.status_code in (200, 201):
                return resp.json()["id"]
        except Exception:
            pass
        return None

    def attach_icon_to_policy(self, policy_id: int, icon_id: int) -> None:
        self._ensure_token()
        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<policy>
  <self_service>
    <self_service_icon><id>{icon_id}</id></self_service_icon>
  </self_service>
</policy>"""
        resp = self.session.put(
            f"{self.base_url}/JSSResource/policies/id/{policy_id}",
            headers=_XML_HEADERS,
            data=payload.encode("utf-8"),
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise JamfAPIError(f"Icon attachment failed ({resp.status_code}): {resp.text}")


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _parse_created_id(xml_text: str) -> int:
    """
    Extract the ID from a Classic API creation response.
    Create responses return a minimal element e.g. <script><id>42</id></script>
    or <policy><id>123</id></policy> — the <id> is always a direct child.
    """
    try:
        root = ET.fromstring(xml_text)
        id_el = root.find("id")
        if id_el is not None and id_el.text:
            return int(id_el.text)
    except ET.ParseError:
        pass
    raise JamfAPIError(f"Could not parse ID from response: {xml_text[:200]}")


def _parse_id_at(xml_text: str, *path: str) -> int:
    """
    Walk a tag path in an XML response and return the int value.
    e.g. _parse_id_at(text, "general", "id")  →  <root><general><id>N</id>
         _parse_id_at(text, "id")             →  <root><id>N</id>
    """
    try:
        node = ET.fromstring(xml_text)
        for tag in path:
            node = node.find(tag)
            if node is None:
                raise JamfAPIError(f"Tag <{tag}> not found in: {xml_text[:200]}")
        return int(node.text)
    except ET.ParseError as exc:
        raise JamfAPIError(f"XML parse error: {exc} — body: {xml_text[:200]}")


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
    )


def _build_script_params_xml(label: str, behavior: dict) -> str:
    lines = [f"<parameter4>{_xml_escape(label)}</parameter4>"]
    for param_num, key in [
        (5, "NOTIFY"),
        (6, "BLOCKING_PROCESS_ACTION"),
        (7, "REOPEN"),
        (8, "IGNORE_APP_STORE_APPS"),
        (9, "INSTALL"),
    ]:
        value = behavior.get(key, "")
        if value:
            lines.append(
                f"<parameter{param_num}>{_xml_escape(key)}={_xml_escape(value)}</parameter{param_num}>"
            )
    return "\n      ".join(lines)
