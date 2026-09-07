from flask import Blueprint, jsonify, request

from database import update_game_data

bp = Blueprint('xbox', __name__, url_prefix='/api/xbox',
               template_folder='templates')


@bp.route('/auth-url')
def auth_url():
    from .xbox_live import get_auth_url
    return jsonify({'url': get_auth_url()})


@bp.route('/connect', methods=['POST'])
def connect():
    from .xbox_live import connect as _connect
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'No code provided'}), 400
    ok, msg = _connect(code)
    if not ok:
        return jsonify({'error': msg}), 401
    return jsonify({'status': 'connected', 'username': msg})


@bp.route('/disconnect', methods=['POST'])
def disconnect():
    from .xbox_live import disconnect as _disconnect
    _disconnect()
    return jsonify({'status': 'disconnected'})


@bp.route('/status')
def status():
    from .xbox_live import is_connected, get_username
    connected = is_connected()
    return jsonify({
        'connected': connected,
        'username':  get_username() if connected else None,
    })


@bp.route('/sync', methods=['POST'])
def sync():
    from .xbox_live import start_library_sync, is_connected
    if not is_connected():
        return jsonify({'error': 'Not connected'}), 401
    return jsonify(start_library_sync())


@bp.route('/sync/status')
def sync_status():
    from .xbox_live import get_sync_state
    return jsonify(get_sync_state())


@bp.route('/sync/cancel', methods=['POST'])
def sync_cancel():
    from .xbox_live import cancel_library_sync
    cancel_library_sync()
    return jsonify({'status': 'ok'})


@bp.route('/uninstall/<int:appid>', methods=['POST'])
def uninstall(appid):
    import plugins
    plugin = plugins.get('xbox')
    if plugin is None:
        return jsonify({'status': 'error', 'message': 'Plugin not loaded'}), 500
    return jsonify(plugin.uninstall_game(appid))


@bp.route('/open-launcher', methods=['POST'])
def open_launcher():
    import plugins
    plugin = plugins.get('xbox')
    if plugin is None:
        return jsonify({'status': 'error', 'message': 'Plugin not loaded'}), 500
    return jsonify(plugin.open_xbox_app())


@bp.route('/scrape-single/<int:appid>', methods=['POST'])
def scrape_single(appid):
    from .xbox_live import scrape_single as _scrape_single
    meta = _scrape_single(appid)
    if meta is None:
        return jsonify({'status': 'error', 'message': 'Game not found'}), 404
    update_game_data(appid, **meta)
    return jsonify({'status': 'success', 'data': meta})
