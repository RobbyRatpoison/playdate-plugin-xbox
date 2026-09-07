"""
windows_uwp.py -- native Windows install/launch/uninstall/install-detection
for Xbox/Microsoft Store (UWP/MSIX) titles, via PowerShell's Appx module.

Windows-only by necessity, not by choice: Wine has no AppX/MSIX deployment
service, WinRT activation, or GDK licensing support at all (confirmed via
live community research, see xbox_live.py's module docstring) -- there is no
equivalent path on Linux/Mac, matching how Playnite's own Xbox plugin behaves.

**Unverified against a real Windows machine or Game Pass account.** Written
from Microsoft's documented Appx cmdlets (Get-AppxPackage, Get-StartApps,
Remove-AppxPackage) and its own "Automate launching UWP apps" guide -- no
Windows box was available to test this against live. Confirm live before
trusting it, same as the rest of this plugin was before its first real run.

Install: there is no supported way to trigger an unattended install of
licensed Store/Game Pass content -- Add-AppxPackage sideloads unsigned/
enterprise packages, not DRM-licensed ones. Matches this codebase's EA App/
Ubisoft precedent (install/launch just opens the platform's own client):
here that means opening ms-windows-store://pdp/?productid=<id> so the Store
handles the real download/license/install flow itself.
"""

import json
import logging
import subprocess

log = logging.getLogger(__name__)

_PS_TIMEOUT = 20


def _run_ps(script, timeout=_PS_TIMEOUT):
    try:
        return subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:
        log.warning('Xbox: PowerShell call failed: %s', e)
        return None


def find_installed_package(pfn):
    """Return {'PackageFullName', 'InstallLocation'} for an installed package
    family (PackageFamilyName, e.g. 'Microsoft.SeaofThieves_8wekyb3d8bbwe'),
    or None if not installed. Per-user query -- no admin needed for the
    current user's own Store installs."""
    script = (
        f"Get-AppxPackage | Where-Object {{$_.PackageFamilyName -eq '{pfn}'}} "
        "| Select-Object PackageFullName, InstallLocation | ConvertTo-Json -Compress"
    )
    result = _run_ps(script)
    if not result or result.returncode != 0:
        return None
    out = (result.stdout or '').strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        log.warning('Xbox: unexpected Get-AppxPackage output: %r', out[:200])
        return None
    return data[0] if isinstance(data, list) else data


def find_aumid(pfn):
    """Resolve the launchable Application User Model ID (PackageFamilyName!AppId)
    via Get-StartApps -- Microsoft's documented way to get the exact AppId
    suffix without hand-parsing the AppX manifest (the AppId is often not
    literally 'App' the way some guides assume)."""
    script = (
        f"Get-StartApps | Where-Object {{$_.AppID -like '{pfn}!*'}} "
        "| Select-Object -First 1 -ExpandProperty AppID"
    )
    result = _run_ps(script)
    if not result or result.returncode != 0:
        return None
    aumid = (result.stdout or '').strip()
    return aumid or None


def launch(pfn):
    """Launch an installed UWP title via shell:AppsFolder -- the same
    mechanism the Start Menu itself uses to launch any UWP app."""
    aumid = find_aumid(pfn)
    if not aumid:
        return False, 'Could not find this game in the Start Menu — is it still installed?'
    try:
        subprocess.Popen(['explorer.exe', f'shell:AppsFolder\\{aumid}'])
        return True, ''
    except Exception as e:
        log.warning('Xbox: launch failed for %r: %s', pfn, e)
        return False, f'Launch failed: {e}'


# Microsoft's own first-party package family name for the Xbox app
# (Microsoft.GamingApp), stable across Windows 10/11 installs -- unlike a
# per-game pfn this is a fixed Microsoft constant, safe to hardcode. Used for
# an "Open Xbox App" convenience button (browse/manage Game Pass) -- this
# plugin's own install/launch/uninstall never routes through the Xbox app
# itself, so it's opened via the same shell:AppsFolder mechanism as any
# other title rather than a guessed, unverified ms-xbl:-style protocol
# (which is per-title deep-linking, not a general "open the app" command,
# and documented as console-only besides).
XBOX_APP_PFN = 'Microsoft.GamingApp_8wekyb3d8bbwe'


def open_xbox_app():
    return launch(XBOX_APP_PFN)


def uninstall(pfn):
    """Remove-AppxPackage for the current user. No admin needed -- only
    -AllUsers or another profile's packages require elevation."""
    script = (
        f"$pkg = Get-AppxPackage | Where-Object {{$_.PackageFamilyName -eq '{pfn}'}}; "
        "if ($pkg) { $pkg | Remove-AppxPackage; 'ok' } else { 'notfound' }"
    )
    result = _run_ps(script, timeout=60)
    if not result:
        return False, 'Uninstall failed: could not run PowerShell'
    out = (result.stdout or '').strip()
    if out == 'ok':
        return True, ''
    if out == 'notfound':
        return True, ''  # already gone -- treat as success, matches other plugins' idempotent uninstall
    return False, (result.stderr or 'Uninstall failed').strip()[:300]


def resync_installed():
    """Refresh installed/install_path for every Xbox game with a known pfn
    (platform_appname). Mirrors the resync_installed() contract other
    plugins implement (see PLUGINS.md) -- called at startup and after
    bulk_rescrape_games()/backup restore."""
    from database import get_db
    db = get_db()
    try:
        rows = db.execute(
            "SELECT appid, platform_appname FROM games "
            "WHERE platform = 'xbox' AND platform_appname IS NOT NULL AND platform_appname != ''"
        ).fetchall()
        for row in rows:
            pkg = find_installed_package(row['platform_appname'])
            if pkg:
                db.execute(
                    "UPDATE games SET installed = 1, install_path = ? WHERE appid = ?",
                    (pkg.get('InstallLocation') or '', row['appid']),
                )
            else:
                db.execute("UPDATE games SET installed = 0 WHERE appid = ?", (row['appid'],))
        db.commit()
        log.info('Xbox: resync_installed checked %d games', len(rows))
    finally:
        db.close()
