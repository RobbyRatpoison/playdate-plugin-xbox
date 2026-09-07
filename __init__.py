import logging
import sys

log = logging.getLogger(__name__)


class XboxPlugin:
    id       = 'xbox'
    name     = 'Xbox / Game Pass for PC'
    platform = 'xbox'
    label    = 'Xbox'

    def register(self, app):
        from .routes import bp
        app.register_blueprint(bp)
        log.info('Xbox plugin registered')

    def on_startup(self):
        if sys.platform == 'win32':
            from .windows_uwp import resync_installed
            try:
                resync_installed()
            except Exception as e:
                log.warning('Xbox: startup resync_installed failed: %s', e)

    def on_shutdown(self):
        pass

    def on_uninstall(self):
        from .xbox_live import disconnect
        disconnect()

    def fetch_description(self, appid, platform_id):
        from .xbox_live import fetch_description
        return fetch_description(appid, platform_id)

    def rescrape(self, appid):
        from .xbox_live import scrape_single
        return scrape_single(appid) or None

    def resync_installed(self):
        if sys.platform != 'win32':
            return
        from .windows_uwp import resync_installed
        resync_installed()

    def _xbox_row(self, appid):
        from database import get_db
        db = get_db()
        row = db.execute(
            "SELECT platform_appname, platform_slug, name FROM games "
            "WHERE appid = ? AND platform = 'xbox'", (appid,)
        ).fetchone()
        db.close()
        return row

    def launch_game(self, appid):
        if sys.platform != 'win32':
            return {'status': 'error', 'message':
                     'Xbox/Microsoft Store games can only be installed and launched on '
                     'Windows -- native UWP titles have no equivalent under Wine.'}

        row = self._xbox_row(appid)
        if not row or not row['platform_appname']:
            return {'status': 'error', 'message': 'Missing package info for this game.'}

        from .windows_uwp import launch, find_installed_package
        pfn = row['platform_appname']
        if not find_installed_package(pfn):
            if row['platform_slug']:
                try:
                    import os
                    os.startfile(f"ms-windows-store://pdp/?productid={row['platform_slug']}")
                except Exception as e:
                    log.warning('Xbox: could not open Store page: %s', e)
                return {'status': 'error', 'message':
                         'Not installed -- opened the Microsoft Store so you can install it. '
                         'Launch again once it finishes.'}
            return {'status': 'error', 'message': 'Game is not installed and has no Store link.'}

        ok, msg = launch(pfn)
        if ok:
            return {'status': 'success'}
        return {'status': 'error', 'message': msg}

    def open_xbox_app(self):
        if sys.platform != 'win32':
            return {'status': 'error', 'message': 'Windows only.'}
        from .windows_uwp import open_xbox_app
        ok, msg = open_xbox_app()
        if ok:
            return {'status': 'success'}
        return {'status': 'error', 'message': msg}

    def uninstall_game(self, appid):
        if sys.platform != 'win32':
            return {'status': 'error', 'message': 'Windows only.'}

        row = self._xbox_row(appid)
        if not row or not row['platform_appname']:
            return {'status': 'error', 'message': 'Missing package info for this game.'}

        from .windows_uwp import uninstall
        ok, msg = uninstall(row['platform_appname'])
        if ok:
            from database import update_game_data
            update_game_data(appid, installed=0, install_path='')
            return {'status': 'success'}
        return {'status': 'error', 'message': msg}

    def js_api(self):
        return {
            'scrape_url':      '/api/xbox/scrape-single/{appid}',
            'scrape_method':   'POST',
            'appid_label':     'Xbox Title ID:',
            'sync_label':      'Sync Xbox Library',
            'store_url':       'https://www.xbox.com/en-US/games/store/game/{slug}',
            'store_label':     'View on Xbox ↗',
            'uninstall_url':   '/api/xbox/uninstall/{appid}',
            'uninstall_confirm': 'Uninstall this game? This will remove it from your PC '
                                  '(Windows only -- no effect on your Xbox/Game Pass access).',
        }

    def manage_ui(self):
        return {
            'sections': [
                {
                    'title': 'Account',
                    'auth': {
                        'endpoint': '/api/xbox/status',
                        'disconnected': [
                            {'type': 'text', 'content':
                                'Connect your Microsoft account to import your Xbox / Game Pass for PC '
                                'library. Install/launch/uninstall are Windows-only -- native UWP titles '
                                'can\'t run under Wine. Note: Microsoft doesn\'t expose current ownership '
                                'status, so every Game Pass title you\'ve ever played will be included on '
                                'sync, even ones you no longer have access to.'},
                            {'type': 'button', 'label': 'Connect Xbox Account', 'action': {
                                'type': 'oauth_popup',
                                'title': 'Connect Xbox',
                                'url_endpoint': '/api/xbox/auth-url',
                                'callback_endpoint': '/api/xbox/connect',
                                'redirect_pattern': 'login.live.com/oauth20_desktop.srf',
                                'code_js': '',
                                'instructions': [
                                    'Sign in with your Microsoft account in the popup.',
                                ],
                                'input_placeholder': '',
                                'open_label': 'Open Sign In',
                                'submit_label': 'Connect',
                            }},
                        ],
                        'connected': [
                            {'type': 'connected_label'},
                            {'type': 'button', 'label': 'Sync Library', 'action': {'type': 'call', 'fn': 'xboxSync'}},
                            {'type': 'button', 'label': 'Open Xbox App', 'variant': 'muted', 'action': {
                                'type': 'call', 'fn': 'xboxOpenApp'}},
                            {'type': 'button', 'label': 'Disconnect', 'variant': 'muted', 'action': {
                                'type': 'post', 'endpoint': '/api/xbox/disconnect',
                                'on_success': 'refresh_auth',
                            }},
                            {'type': 'status_output', 'key': 'main'},
                        ],
                    },
                },
            ],
        }

    def fragments(self):
        return {
            'tools_scripts': 'xbox_tools_scripts.html',
        }


plugin = XboxPlugin()
