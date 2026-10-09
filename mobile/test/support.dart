import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:orgbots/src/api.dart';
import 'package:orgbots/src/bot3d/snapshots.dart';
import 'package:orgbots/src/session.dart';
import 'package:orgbots/src/theme.dart';
import 'package:orgbots/src/web_views.dart';

/// The screens against a fake Orgbots server that answers the way the real one does,
/// recording every write the app makes.
String ago(Duration d) => DateTime.now().subtract(d).toIso8601String();

Map<String, dynamic> message(
  int seq,
  String role,
  String content, [
  Map<String, dynamic>? payload,
]) => {
  'id': 'm$seq',
  'seq': seq,
  'bot_id': 'b1',
  'role': role,
  'content': content,
  'payload': payload ?? {},
  'created_at': ago(const Duration(minutes: 5)),
  'reactions': <String>[],
};

final bots = [
  {
    'id': 'b1',
    'name': 'Price Tracker',
    'label': 'Shopping',
    'appearance': <String, dynamic>{},
    'brief': {'mission': 'Find the best price.'},
    'pinned': false,
    'working': false,
    'needs_attention': false,
    'unread': false,
    'updated_at': ago(const Duration(minutes: 5)),
    'last_message': message(9, 'bot', 'The cheapest is **£23.21**'),
  },
  {
    'id': 'b2',
    'name': 'Researcher',
    'label': 'Web research',
    'appearance': <String, dynamic>{},
    'brief': <String, dynamic>{},
    'pinned': false,
    'working': true,
    'needs_attention': true,
    'unread': true,
    'updated_at': ago(const Duration(hours: 2)),
    'last_message': null,
  },
];

final transcript = [
  message(1, 'user', 'Find the cheapest travel book'),
  message(2, 'activity', '', {
    'action': {'type': 'navigate', 'url': 'https://books.toscrape.com'},
    'ok': true,
  }),
  message(3, 'activity', '', {
    'action': {'type': 'click', 'element': 3, 'element_label': 'Travel'},
    'ok': false,
    'error': '504',
  }),
  message(4, 'bot', 'The cheapest is **The Road to Little Dribbling** at £23.21.'),
  message(5, 'approval', 'This places an order.', {
    'pending_id': 'p1',
    'action': {
      'type': 'click',
      'element': 7,
      'element_label': 'Buy now',
      'page_url': 'https://books.toscrape.com/x',
    },
  }),
  message(6, 'credentials', 'The store wants me to sign in.', {
    'credential_request_id': 'c1',
    'host': 'books.toscrape.com',
    'purpose': 'sign_in',
    'fields': [
      {'key': 'email', 'kind': 'email', 'label': 'Email'},
      {'key': 'pw', 'kind': 'password', 'label': 'Password'},
    ],
    'saved': <dynamic>[],
  }),
];

/// A bot built to break layouts: a very long name, label and mission, a long URL with
/// no spaces, a reply with long lines, code and lists, and every kind of card.
const longName = 'Quarterly Competitive Intelligence and Market Research Assistant';

const longUrl =
    'https://www.example-marketplace.com/catalogue/category/books/travel_2/index.html?utm_source=orgbots&utm_medium=bot&session=abcdefghijklmnopqrstuvwxyz0123456789';

final awkwardBot = {
  'id': 'b3',
  'name': longName,
  'label': 'Market research, pricing analysis and weekly competitor summaries for the team',
  'appearance': <String, dynamic>{},
  'brief': {
    'mission': 'Track every competitor price change across twelve marketplaces, summarise what moved and why, and flag anything that needs a decision from me before Friday.',
    'duties': [
      'Watch competitor pricing pages daily and record every change with a screenshot',
      'Summarise the week',
      'Draft the Friday report',
    ],
  },
  'pinned': true,
  'working': true,
  'needs_attention': false,
  'unread': true,
  'visibility': 'team',
  'parent_bot_id': 'b1',
  'updated_at': ago(const Duration(days: 3)),
  'last_message': message(
    3,
    'bot',
    'A very long last message that should be truncated to one line in the list without overflowing anything at all',
  ),
};

final awkwardTranscript = [
  message(
    1,
    'user',
    'Compare these: $longUrl and tell me which is cheaper, with the full breakdown please.',
    {
      'attachments': [
        {
          'id': 'f1',
          'name': 'a-very-long-screenshot-file-name-from-the-phone-camera-roll-2026-10-09.jpg',
          'media_type': 'image/jpeg',
        },
        {'id': 'f2', 'name': 'notes.pdf', 'media_type': 'application/pdf'},
      ],
      'from': {'member_id': 'u', 'name': 'Sam Example-Longsurname'},
    },
  ),
  for (var i = 0; i < 9; i++)
    message(2 + i, 'activity', 'Looking at $longUrl to read the price table', {
      'action': {
        'type': i.isEven ? 'navigate' : 'type',
        'url': longUrl,
        'text': 'search terms that go on and on',
        'element_label': 'Search the entire catalogue by keyword or ISBN',
      },
      'ok': i != 3,
      'error': 'net::ERR_TIMED_OUT while loading $longUrl',
    }),
  message(
    11,
    'bot',
    '## Summary of every marketplace\n\n'
        'The cheapest is **The Road to Little Dribbling: Adventures of an American in Britain '
        '(Notes From a Small Island #2)** at £23.21, see $longUrl for the listing.\n\n'
        '- First option with a long explanation that wraps onto several lines on a small phone\n'
        '- `a_really_long_identifier_without_any_spaces_that_must_wrap_somewhere_sensible`\n'
        '1. Numbered item one\n'
        '2. Numbered item two',
  ),
  message(
    12,
    'error',
    'The run stopped: the model provider returned an error after three retries ($longUrl).',
  ),
  message(13, 'system', 'Sam Example-Longsurname took control of the screen and handed it back.'),
  message(
    14,
    'approval',
    'Submitting this form places an order of £23.21 on your saved card, which cannot be undone once the store confirms it.',
    {
      'pending_id': 'p3',
      'action': {
        'type': 'type',
        'element': 7,
        'element_label': 'Card number field on the checkout page with a long label',
        'text': 'a long piece of text the bot wants to type into the field',
        'url': longUrl,
        'page_url': longUrl,
      },
    },
  ),
  message(
    15,
    'credentials',
    'The marketplace with the cheapest copy wants me to sign in before it shows the basket.',
    {
      'credential_request_id': 'c3',
      'host': 'accounts.example-marketplace-with-a-long-domain.com',
      'purpose': 'sign_up',
      'retry': true,
      'fields': [
        {'key': 'name', 'kind': 'name', 'label': 'Your full name as it appears on your card'},
        {'key': 'email', 'kind': 'email', 'label': 'Email'},
        {'key': 'pw', 'kind': 'new_password', 'label': 'Choose a password'},
        {'key': 'otp', 'kind': 'otp', 'label': 'Code'},
      ],
      'saved': [
        {'id': 's1', 'label': 'sam.example-longsurname@example-company-domain.com'},
      ],
    },
  ),
];

class FakeServer {
  FakeServer({this.failMessages = false, this.noBots = false, this.down = false});

  /// A new server: nobody has made a bot yet.
  final bool noBots;

  /// Every request fails, as when the server is unreachable.
  final bool down;

  /// Every message poll fails, as when the server goes away mid-conversation.
  bool failMessages;
  final writes = <(String, String, Object?)>[];

  late final client = MockClient((request) async {
    final path = request.url.path.replaceFirst('/rt/v1', '');
    if (request.method != 'GET') {
      writes.add((request.method, path, request.body.isEmpty ? null : jsonDecode(request.body)));
    }
    if (down) return http.Response('{"detail":"runtime API unreachable"}', 502);
    final all = noBots ? <Map<String, dynamic>>[] : [...bots, awkwardBot];
    final bot = RegExp(r'^/bots/(\w+)$').firstMatch(path);
    final messages = RegExp(r'^/bots/(\w+)/messages$').firstMatch(path);
    Object body;
    if (path == '/bots') {
      body = {'organization_id': 'o', 'bots': all};
    } else if (bot != null && request.method == 'GET') {
      body = all.firstWhere((b) => b['id'] == bot[1]);
    } else if (messages != null && request.method == 'GET') {
      if (failMessages) return http.Response('{"detail":"upstream unavailable"}', 502);
      final id = messages[1];
      final after = int.parse(request.url.queryParameters['after'] ?? '0');
      final list = switch (id) {
        'b1' => transcript,
        'b3' => awkwardTranscript,
        _ => <Map<String, dynamic>>[],
      };
      body = {
        'messages': list.where((m) => (m['seq'] as int) > after).toList(),
        'pending': ['p1', 'p3'],
        'credential_requests': ['c1', 'c3'],
        'working': id == 'b3',
        'run_status': null,
      };
    } else {
      body = {'ok': true, 'admitted': true, 'run_id': 'r', 'refusal_reason': null};
    }
    return http.Response(jsonEncode(body), 200, headers: {'content-type': 'application/json'});
  });
}

/// A phone-width screen tall enough to lay out a whole test conversation at once.
Future<(Session, FakeServer)> pump(WidgetTester tester, Widget screen) =>
    pumpAt(tester, screen, size: const Size(390, 3000));

/// [screen] at a logical [size], [textScale] and [palette], against a fake server.
Future<(Session, FakeServer)> pumpAt(
  WidgetTester tester,
  Widget screen, {
  Size size = const Size(390, 844),
  double textScale = 1,
  Palette palette = Palette.dark,
  FakeServer? server,
}) async {
  BotSnapshots.instance.enabled = false;
  WebViews.enabled = false;
  server ??= FakeServer();
  final session = Session(api: Api(client: server.client))
    ..api.server = 'https://bots.example.com'
    ..status = Status.ready;
  tester.view.physicalSize = size * 3;
  tester.view.devicePixelRatio = 3;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    SessionScope(
      session: session,
      child: MaterialApp(
        theme: themeFor(palette),
        home: Builder(
          builder: (context) => MediaQuery(
            data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(textScale)),
            child: screen,
          ),
        ),
      ),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
  return (session, server);
}

/// The real Roboto, from the Flutter SDK, so text in tests measures as it does on a
/// phone (the default test font draws every character as a full square).
Future<void> loadRealFonts() async {
  // `flutter test` sets FLUTTER_ROOT; Roboto ships with every SDK.
  final dir = Directory(
    '${Platform.environment['FLUTTER_ROOT']}/bin/cache/artifacts/material_fonts',
  );
  final roboto = FontLoader('Roboto');
  for (final weight in ['Regular', 'Medium', 'Bold']) {
    final file = File('${dir.path}/Roboto-$weight.ttf');
    if (!file.existsSync()) {
      throw StateError('Roboto not found at ${file.path}; run the tests with `flutter test`');
    }
    roboto.addFont(Future.value(ByteData.sublistView(file.readAsBytesSync())));
  }
  await roboto.load();
}
