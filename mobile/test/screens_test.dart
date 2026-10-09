import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:orgbots/main.dart';
import 'package:orgbots/src/api.dart';
import 'package:orgbots/src/bot3d/snapshots.dart';
import 'package:orgbots/src/screens/chat.dart';
import 'package:orgbots/src/screens/home.dart';
import 'package:orgbots/src/session.dart';
import 'package:orgbots/src/theme.dart';

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

class FakeServer {
  final writes = <(String, String, Object?)>[];

  late final client = MockClient((request) async {
    final path = request.url.path.replaceFirst('/rt/v1', '');
    if (request.method != 'GET') {
      writes.add((request.method, path, request.body.isEmpty ? null : jsonDecode(request.body)));
    }
    Object body;
    if (path == '/bots') {
      body = {'organization_id': 'o', 'bots': bots};
    } else if (path == '/bots/b1') {
      body = bots.first;
    } else if (path == '/bots/b1/messages' && request.method == 'GET') {
      final after = int.parse(request.url.queryParameters['after'] ?? '0');
      body = {
        'messages': transcript.where((m) => (m['seq'] as int) > after).toList(),
        'pending': ['p1'],
        'credential_requests': ['c1'],
        'working': false,
        'run_status': null,
      };
    } else {
      body = {'ok': true, 'admitted': true, 'run_id': 'r', 'refusal_reason': null};
    }
    return http.Response(jsonEncode(body), 200, headers: {'content-type': 'application/json'});
  });
}

/// A phone-width screen tall enough to lay out a whole test conversation at once.
Future<(Session, FakeServer)> pump(WidgetTester tester, Widget screen) async {
  BotSnapshots.instance.enabled = false;
  final server = FakeServer();
  final session = Session(api: Api(client: server.client))
    ..api.server = 'https://bots.example.com'
    ..status = Status.ready;
  tester.view.physicalSize = const Size(1170, 9000);
  tester.view.devicePixelRatio = 3;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    SessionScope(
      session: session,
      child: MaterialApp(theme: themeFor(Palette.dark), home: screen),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
  return (session, server);
}

void main() {
  testWidgets('home lists bots, the one waiting on you first', (tester) async {
    await pump(tester, const HomeScreen());
    expect(find.text('Researcher'), findsOneWidget);
    expect(find.text('Price Tracker'), findsOneWidget);
    expect(find.text('Waiting for you'), findsOneWidget);
    expect(find.text('The cheapest is £23.21'), findsOneWidget);
    final researcher = tester.getTopLeft(find.text('Researcher')).dy;
    final tracker = tester.getTopLeft(find.text('Price Tracker')).dy;
    expect(researcher, lessThan(tracker));

    await tester.enterText(find.byType(TextField), 'price');
    await tester.pump();
    expect(find.text('Researcher'), findsNothing);
    expect(find.text('Price Tracker'), findsOneWidget);
  });

  testWidgets('chat folds steps, renders replies and asks with cards', (tester) async {
    final (_, server) = await pump(tester, ChatScreen(bot: Bot.fromJson(bots.first)));
    await tester.pump(const Duration(milliseconds: 100));

    expect(server.writes.any((w) => w.$2 == '/bots/b1/read'), isTrue);
    expect(find.text('Sign in to books.toscrape.com'), findsOneWidget);
    expect(find.text('Needs approval'), findsOneWidget);
    expect(find.text('Click “Buy now”'), findsOneWidget);
    expect(find.text('Worked · 2 steps · 1 retried'), findsOneWidget);
    expect(
      find.text('The cheapest is The Road to Little Dribbling at £23.21.', findRichText: true),
      findsWidgets,
    );
    expect(find.text('Find the cheapest travel book'), findsOneWidget);

    await tester.tap(find.text('Worked · 2 steps · 1 retried'));
    await tester.pumpAndSettle(const Duration(milliseconds: 50));
    expect(find.text('Open https://books.toscrape.com'), findsOneWidget);
    expect(find.text('Click “Travel”'), findsOneWidget);
  });

  testWidgets('approving sends the decision', (tester) async {
    final (_, server) = await pump(tester, ChatScreen(bot: Bot.fromJson(bots.first)));
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.text('Allow once'), findsOneWidget);
    await tester.tap(find.text('Allow once'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(
      server.writes,
      contains(
        predicate<(String, String, Object?)>(
          (w) => w.$2 == '/bots/b1/pending/p1' && (w.$3 as Map)['decision'] == 'once',
        ),
      ),
    );
  });

  testWidgets('a sign-in card sends the fields once, then clears them', (tester) async {
    final (_, server) = await pump(tester, ChatScreen(bot: Bot.fromJson(bots.first)));
    await tester.pump(const Duration(milliseconds: 100));
    final fields = find.descendant(of: find.byType(ListView), matching: find.byType(TextField));
    expect(fields, findsNWidgets(2));
    await tester.enterText(fields.at(0), 'me@example.com');
    await tester.enterText(fields.at(1), 'hunter2');
    await tester.tap(find.text('Sign in'));
    await tester.pump(const Duration(milliseconds: 100));
    final sent = server.writes.firstWhere((w) => w.$2 == '/bots/b1/credentials/c1');
    expect(sent.$3, {
      'values': {'email': 'me@example.com', 'pw': 'hunter2'},
      'save': true,
    });
    expect(find.text('hunter2'), findsNothing);
  });

  testWidgets('sending a message posts it and clears the composer', (tester) async {
    final (_, server) = await pump(tester, ChatScreen(bot: Bot.fromJson(bots.first)));
    await tester.pump(const Duration(milliseconds: 100));
    final composer = find.widgetWithText(TextField, 'Ask Price Tracker anything');
    await tester.enterText(composer, 'Thanks — order it');
    await tester.pump();
    await tester.tap(find.byTooltip('Send'));
    await tester.pump(const Duration(milliseconds: 100));
    final sent = server.writes.firstWhere((w) => w.$2 == '/bots/b1/messages');
    expect((sent.$3 as Map)['text'], 'Thanks — order it');
    expect(find.text('Thanks — order it'), findsNothing);
  });

  testWidgets('ending the session closes an open chat', (tester) async {
    FlutterSecureStorage.setMockInitialValues({});
    BotSnapshots.instance.enabled = false;
    final server = FakeServer();
    final session = Session(api: Api(client: server.client))
      ..api.server = 'https://bots.example.com'
      ..status = Status.ready;
    tester.view.physicalSize = const Size(1170, 2532);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(OrgbotsApp(session: session));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.text('Price Tracker'));
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
    expect(find.byType(ChatScreen), findsOneWidget);

    await session.disconnect();
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
    expect(find.byType(ChatScreen), findsNothing);
    expect(find.text('Server address'), findsOneWidget);
    expect(server.writes.any((w) => w.$2 == '/auth/sign-out'), isFalse);
  });
}
