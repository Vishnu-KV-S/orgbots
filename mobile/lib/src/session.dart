import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:webview_flutter/webview_flutter.dart';

import 'api.dart';

/// Which server this phone uses, and who is signed in to it.
///
/// [status] picks the first screen: no server yet → connect; a server with members
/// and no session → sign in; otherwise the bots. The server address and the session
/// token live in the OS keychain.
enum Status { loading, noServer, signedOut, ready, offline }

class Session extends ChangeNotifier {
  Session({Api? api, FlutterSecureStorage? storage})
    : api = api ?? Api(),
      _storage = storage ?? const FlutterSecureStorage() {
    this.api.onUnauthorized = () {
      if (status == Status.signedOut) return;
      me = null;
      _set(Status.signedOut);
    };
  }

  static const _serverKey = 'orgbots.server';
  static const _sessionKey = 'orgbots.session';

  final Api api;
  final FlutterSecureStorage _storage;

  Status status = Status.loading;
  String mode = 'none';
  Me? me;
  String? error;

  String get server => api.server;

  void _set(Status next) {
    status = next;
    notifyListeners();
  }

  Future<void> load() async {
    String? saved;
    try {
      saved = await _storage.read(key: _serverKey);
      api.session = await _storage.read(key: _sessionKey);
    } catch (_) {
      saved = null;
    }
    if (saved == null) return _set(Status.noServer);
    api.server = saved;
    await refresh();
  }

  /// Ask the server again who we are: after signing in, or to retry when offline.
  Future<Status> refresh() async {
    Status next;
    try {
      final who = await api.authMe();
      mode = who.mode;
      me = who.member;
      error = null;
      next = who.mode == 'members' && who.member == null ? Status.signedOut : Status.ready;
    } on ApiError catch (e) {
      error = e.message;
      next = Status.offline;
    }
    _set(next);
    return next;
  }

  /// Probe a server and, if it answers as Orgbots, make it this phone's.
  Future<void> connect(String input) async {
    final url = normalizeServer(input);
    if (url == null) {
      throw ApiError(
        input.contains('@')
            ? 'That’s an email address. Enter your Orgbots server’s address here, like bots.example.com or your computer’s Wi‑Fi address, 192.168.x.x:3000; you sign in on the next screen.'
            : 'That doesn’t look like a server address',
        0,
      );
    }
    final previous = api.server;
    api.server = url;
    api.session = null;
    try {
      final who = await api.authMe();
      mode = who.mode;
      me = who.member;
    } on ApiError catch (e) {
      api.server = previous;
      throw ApiError(
        '${e.message}. Check the address and that the Orgbots web app is running there.',
        e.status,
      );
    }
    await _storage.write(key: _serverKey, value: url);
    await _storage.delete(key: _sessionKey);
    error = null;
    _set(mode == 'members' && me == null ? Status.signedOut : Status.ready);
  }

  /// The sign-in page let us in: take its session cookie from the web view's store and
  /// check it with the server. The web view hands cookies over asynchronously on iOS,
  /// so this tries a few times before giving up.
  Future<bool> adoptWebSession() async {
    final cookies = WebViewCookieManager();
    for (var attempt = 0; attempt < 6; attempt++) {
      if (!kIsWeb) {
        try {
          final found = await cookies.getCookies(domain: Uri.parse(server));
          final token = found.where((c) => c.name == sessionCookie).map((c) => c.value).firstOrNull;
          if (token != null && token.isNotEmpty) {
            api.session = token;
            await _storage.write(key: _sessionKey, value: token);
          }
        } catch (_) {
          // Not available on this platform: the request may still carry it.
        }
      }
      if (await refresh() != Status.signedOut) return true;
      await Future<void>.delayed(const Duration(milliseconds: 500));
    }
    return false;
  }

  /// Put the session into the web views' cookie store, for pages that need it (the
  /// live screen) after the store was cleared.
  Future<void> shareSessionWithWebViews() async {
    final token = api.session;
    if (token == null || kIsWeb) return;
    final uri = Uri.parse(server);
    try {
      await WebViewCookieManager().setCookie(
        WebViewCookie(name: sessionCookie, value: token, domain: uri.host),
      );
    } catch (_) {
      // Best effort: the sign-in already left it there.
    }
  }

  Future<void> signOut() async {
    try {
      await api.signOut();
    } catch (_) {
      // Signing out locally is what matters.
    }
    await _forgetSession();
    _set(mode == 'members' ? Status.signedOut : Status.ready);
  }

  /// Forget the server altogether.
  Future<void> disconnect() async {
    if (mode == 'members') {
      try {
        await api.signOut();
      } catch (_) {}
    }
    await _forgetSession();
    await _storage.delete(key: _serverKey);
    api.server = '';
    _set(Status.noServer);
  }

  Future<void> _forgetSession() async {
    me = null;
    api.session = null;
    await _storage.delete(key: _sessionKey);
    if (!kIsWeb) {
      try {
        await WebViewCookieManager().clearCookies();
      } catch (_) {}
    }
  }
}

/// Makes the [Session] reachable from any widget below it.
class SessionScope extends InheritedNotifier<Session> {
  const SessionScope({super.key, required Session session, required super.child})
    : super(notifier: session);

  static Session of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<SessionScope>()!.notifier!;

  /// Without listening: for callbacks.
  static Session read(BuildContext context) =>
      context.getInheritedWidgetOfExactType<SessionScope>()!.notifier!;
}
