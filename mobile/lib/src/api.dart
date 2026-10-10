import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart' show StringCharacters;
import 'package:http/http.dart' as http;

/// The Orgbots API, as the phone reaches it.
///
/// The app talks to the same web server people open in a browser, through its
/// same-origin proxy (`ui/app/rt/[...path]/route.ts`): `https://server/rt/v1/...`.
/// The API stays a private service behind the web app and the phone needs exactly one
/// address. With members, the session is the web app's own `aor_session` cookie, taken
/// from the sign-in web view and sent as a header here.
///
/// The models are the subset of `ui/lib/api/bots.ts` the app uses, field for field.

const sessionCookie = 'aor_session';

/// The bot's screen is a 1280×800 browser viewport; input is in its pixels.
const viewportWidth = 1280.0;
const viewportHeight = 800.0;

class ApiError implements Exception {
  ApiError(this.message, this.status);
  final String message;
  final int status;

  @override
  String toString() => message;
}

typedef Json = Map<String, dynamic>;

class Api {
  Api({http.Client? client}) : _http = client ?? http.Client();

  final http.Client _http;
  String server = '';
  String? session;

  /// Called on a 401 outside `/auth/`: the session ended.
  VoidCallback? onUnauthorized;

  Uri url(String path) => Uri.parse('$server/rt/v1$path');

  /// Headers that carry the session: for the API, and for images and streams the
  /// app loads from the server.
  Map<String, String> get authHeaders => {
    // A browser (the web build) sends the cookie itself and refuses this header.
    if (session != null && !kIsWeb) 'cookie': '$sessionCookie=$session',
  };

  Future<T> _send<T>(
    String method,
    String path, {
    Object? body,
    Duration timeout = const Duration(seconds: 30),
  }) async {
    if (server.isEmpty) throw ApiError('No server is set', 0);
    final request = http.Request(method, url(path))
      ..headers.addAll({'accept': 'application/json', ...authHeaders});
    if (body != null) {
      request.headers['content-type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    final http.Response response;
    try {
      response = await http.Response.fromStream(await _http.send(request).timeout(timeout));
    } on TimeoutException {
      throw ApiError('$server took too long to answer', 0);
    } catch (_) {
      throw ApiError('Can’t reach $server', 0);
    }
    if (response.statusCode == 401 && !path.startsWith('/auth/')) {
      onUnauthorized?.call();
    }
    if (response.statusCode >= 400) {
      var detail = response.body;
      // Another site's error page (a wrong address) says nothing useful in a notice.
      if (detail.trimLeft().startsWith('<')) detail = '';
      try {
        final decoded = jsonDecode(response.body);
        if (decoded is Map && decoded['detail'] is String) {
          detail = decoded['detail'] as String;
        }
      } catch (_) {
        // A non-JSON error body is still the most useful thing we have.
      }
      throw ApiError(detail.isEmpty ? 'HTTP ${response.statusCode}' : detail, response.statusCode);
    }
    if (response.statusCode == 204 || response.body.isEmpty) return null as T;
    return jsonDecode(utf8.decode(response.bodyBytes)) as T;
  }

  Future<Json> _get(String path, {Duration? timeout}) =>
      _send<Json>('GET', path, timeout: timeout ?? const Duration(seconds: 30));
  Future<Json> _post(String path, [Object? body]) => _send<Json>('POST', path, body: body ?? {});

  // --- endpoints ---------------------------------------------------------------------

  /// Is this an Orgbots server, and who am I on it.
  Future<({String mode, Me? member})> authMe() async {
    final j = await _get('/auth/me', timeout: const Duration(seconds: 10));
    final mode = j['mode'];
    if (mode != 'none' && mode != 'members') {
      throw ApiError('Not an Orgbots server', 0);
    }
    final m = j['member'];
    return (mode: mode as String, member: m is Json ? Me.fromJson(m) : null);
  }

  Future<void> signOut() => _post('/auth/sign-out');

  Future<List<Bot>> listBots() async {
    final j = await _get('/bots');
    return (j['bots'] as List).cast<Json>().map(Bot.fromJson).toList();
  }

  Future<Bot> getBot(String id) async => Bot.fromJson(await _get('/bots/$id'));

  Future<Bot> createBot(Json draft) async => Bot.fromJson(await _post('/bots', draft));

  Future<void> updateBot(String id, Json fields) => _send<Json>('PATCH', '/bots/$id', body: fields);

  Future<void> deleteBot(String id) => _send<Json?>('DELETE', '/bots/$id?with_helpers=false');

  Future<void> markRead(String id) => _post('/bots/$id/read', {'unread': false});

  Future<void> stopBot(String id) => _post('/bots/$id/stop');

  Future<MessagePage> fetchMessages(String id, int after) async =>
      MessagePage.fromJson(await _get('/bots/$id/messages?after=$after'));

  Future<Sent> sendMessage(String id, String text, List<String> attachments) async => Sent.fromJson(
    await _post('/bots/$id/messages', {
      'text': text,
      'reply_to': null,
      'attachments': attachments,
      'voice': false,
    }),
  );

  Future<Sent> decide(String id, String pendingId, String decision) async =>
      Sent.fromJson(await _post('/bots/$id/pending/$pendingId', {'decision': decision}));

  /// The values go in this one request body and nowhere else.
  Future<void> submitCredentials(
    String id,
    String requestId,
    Map<String, String> values,
    bool save,
  ) => _post('/bots/$id/credentials/$requestId', {'values': values, 'save': save});

  Future<void> chooseSavedLogin(String id, String requestId, String entryId) =>
      _post('/bots/$id/credentials/$requestId', {'use_entry_id': entryId});

  Future<void> cancelCredentials(String id, String requestId) =>
      _post('/bots/$id/credentials/$requestId/cancel');

  Future<List<String>> react(String botId, String messageId, String emoji, bool on) async {
    final j = await _post('/bots/$botId/messages/$messageId/reactions', {'emoji': emoji, 'on': on});
    return (j['reactions'] as List).cast<String>();
  }

  /// Upload into the team's drive; a message then carries the returned ids.
  Future<List<Attachment>> uploadFiles(
    String id,
    List<({String name, String mediaType, Uint8List bytes})> files,
  ) async {
    final j = await _send<Json>(
      'POST',
      '/bots/$id/files/upload',
      body: {
        'files': [
          for (final f in files)
            {'name': f.name, 'media_type': f.mediaType, 'data': base64Encode(f.bytes)},
        ],
      },
      timeout: const Duration(minutes: 2),
    );
    return (j['files'] as List).cast<Json>().map(Attachment.fromJson).toList();
  }

  Future<void> setController(String id, String controller) =>
      _post('/bots/$id/computer/control', {'controller': controller});

  Future<String> sendInputs(String id, List<Json> events) async {
    final j = await _post('/bots/$id/computer/inputs', {'events': events});
    return (j['url'] as String?) ?? '';
  }

  Uri screenshotUrl(String botId, String screenshotId) =>
      url('/bots/$botId/screenshots/$screenshotId');

  Uri streamUrl(String botId) => url('/bots/$botId/computer/stream');
}

/// Turn a server address as typed into one we can call: scheme added, path dropped.
/// Without a scheme, a server on this network (a private IP, localhost or `.local`)
/// gets http and anything else https.
String? normalizeServer(String input) {
  var value = input.trim();
  if (value.isEmpty) return null;
  if (!RegExp(r'^https?://', caseSensitive: false).hasMatch(value)) {
    final host = Uri.tryParse('http://$value')?.host ?? '';
    value = '${_isLocalHost(host) ? 'http' : 'https'}://$value';
  }
  final uri = Uri.tryParse(value);
  // `name@host` is an email address (or credentials), never an Orgbots server.
  if (uri == null || uri.host.isEmpty || uri.userInfo.isNotEmpty) return null;
  return '${uri.scheme}://${uri.host}${uri.hasPort ? ':${uri.port}' : ''}';
}

bool _isLocalHost(String host) {
  if (host == 'localhost' || host.endsWith('.local')) return true;
  final ip = host.split('.').map(int.tryParse).toList();
  if (ip.length != 4 || ip.contains(null)) return false;
  final a = ip[0]!, b = ip[1]!;
  return a == 10 || a == 127 || (a == 192 && b == 168) || (a == 172 && b >= 16 && b <= 31);
}

// --- models --------------------------------------------------------------------------

String _s(Object? v) => v is String ? v : '';
bool _b(Object? v) => v == true;
Json _m(Object? v) => v is Json ? v : <String, dynamic>{};

class Me {
  Me({required this.id, required this.email, required this.name, required this.role});
  factory Me.fromJson(Json j) =>
      Me(id: _s(j['id']), email: _s(j['email']), name: _s(j['name']), role: _s(j['role']));
  final String id;
  final String email;
  final String name;
  final String role;

  String get initial => (name.isNotEmpty ? name : email).characters.first.toUpperCase();
}

class Bot {
  Bot({
    required this.id,
    required this.name,
    required this.label,
    required this.description,
    required this.brief,
    required this.appearance,
    required this.pinned,
    required this.hidden,
    required this.unread,
    required this.needsAttention,
    required this.working,
    required this.parentBotId,
    required this.visibility,
    required this.updatedAt,
    required this.lastMessage,
  });

  factory Bot.fromJson(Json j) => Bot(
    id: _s(j['id']),
    name: _s(j['name']),
    label: _s(j['label']),
    description: _s(j['description']),
    brief: _m(j['brief']),
    appearance: _m(j['appearance']),
    pinned: _b(j['pinned']),
    hidden: _b(j['hidden']),
    unread: _b(j['unread']),
    needsAttention: _b(j['needs_attention']),
    working: _b(j['working']),
    parentBotId: j['parent_bot_id'] as String?,
    visibility: _s(j['visibility']),
    updatedAt: DateTime.tryParse(_s(j['updated_at'])) ?? DateTime.now(),
    lastMessage: j['last_message'] is Json ? BotMessage.fromJson(j['last_message'] as Json) : null,
  );

  final String id;
  final String name;
  final String label;
  final String description;
  final Json brief;

  /// The 3D body as saved; `{}` means "derive one from the id", as on the web.
  final Json appearance;
  final bool pinned;
  final bool hidden;
  final bool unread;
  final bool needsAttention;
  final bool working;
  final String? parentBotId;
  final String visibility;
  final DateTime updatedAt;
  final BotMessage? lastMessage;

  String get mission => _s(brief['mission']);
  List<String> get duties =>
      (brief['duties'] is List) ? (brief['duties'] as List).cast<String>() : [];

  DateTime get lastActive => lastMessage?.createdAt ?? updatedAt;

  Bot copyWith({bool? pinned}) => Bot(
    id: id,
    name: name,
    label: label,
    description: description,
    brief: brief,
    appearance: appearance,
    pinned: pinned ?? this.pinned,
    hidden: hidden,
    unread: unread,
    needsAttention: needsAttention,
    working: working,
    parentBotId: parentBotId,
    visibility: visibility,
    updatedAt: updatedAt,
    lastMessage: lastMessage,
  );
}

class CredentialField {
  CredentialField(this.key, this.kind, this.label);
  final String key;
  final String kind;
  final String label;
}

class Attachment {
  Attachment({required this.id, required this.name, required this.mediaType});
  factory Attachment.fromJson(Json j) =>
      Attachment(id: _s(j['id']), name: _s(j['name']), mediaType: _s(j['media_type']));
  final String id;
  final String name;
  final String mediaType;
}

class BotMessage {
  BotMessage({
    required this.id,
    required this.seq,
    required this.role,
    required this.content,
    required this.payload,
    required this.createdAt,
    required this.reactions,
  });

  factory BotMessage.fromJson(Json j) => BotMessage(
    id: _s(j['id']),
    seq: (j['seq'] as num?)?.toInt() ?? 0,
    role: _s(j['role']),
    content: _s(j['content']),
    payload: _m(j['payload']),
    createdAt: DateTime.tryParse(_s(j['created_at'])) ?? DateTime.now(),
    reactions: (j['reactions'] is List) ? (j['reactions'] as List).cast<String>() : const [],
  );

  final String id;
  final int seq;

  /// user | bot | activity | approval | system | error | credentials
  final String role;
  final String content;
  final Json payload;
  final DateTime createdAt;
  final List<String> reactions;

  Json get action => _m(payload['action']);
  String get actionType => _s(action['type']);
  bool? get ok => payload['ok'] as bool?;
  String get error => _s(payload['error']);
  String get pendingId => _s(payload['pending_id']);
  String get credentialRequestId => _s(payload['credential_request_id']);
  String get screenshotId => _s(payload['screenshot_id']);
  String get host => _s(payload['host']);
  String get purpose => payload['purpose'] is String ? payload['purpose'] as String : 'sign_in';
  bool get retry => _b(payload['retry']);
  String get fromName => _s(_m(payload['from'])['name']);
  String get routine => _s(payload['routine']);

  List<CredentialField> get fields => [
    for (final f in (payload['fields'] is List ? payload['fields'] as List : const []))
      if (f is Json) CredentialField(_s(f['key']), _s(f['kind']), _s(f['label'])),
  ];

  List<({String id, String label})> get savedLogins => [
    for (final s in (payload['saved'] is List ? payload['saved'] as List : const []))
      if (s is Json) (id: _s(s['id']), label: _s(s['label'])),
  ];

  List<Attachment> get attachments => [
    for (final a in (payload['attachments'] is List ? payload['attachments'] as List : const []))
      if (a is Json) Attachment.fromJson(a),
  ];
}

class MessagePage {
  MessagePage(this.messages, this.pending, this.asking, this.working);
  factory MessagePage.fromJson(Json j) => MessagePage(
    (j['messages'] as List).cast<Json>().map(BotMessage.fromJson).toList(),
    ((j['pending'] as List?) ?? const []).cast<String>().toSet(),
    ((j['credential_requests'] as List?) ?? const []).cast<String>().toSet(),
    _b(j['working']),
  );
  final List<BotMessage> messages;
  final Set<String> pending;
  final Set<String> asking;
  final bool working;
}

class Sent {
  Sent(this.admitted, this.refusalReason);
  factory Sent.fromJson(Json? j) =>
      Sent(j == null || j['admitted'] != false, j?['refusal_reason'] as String?);
  final bool admitted;
  final String? refusalReason;
}
