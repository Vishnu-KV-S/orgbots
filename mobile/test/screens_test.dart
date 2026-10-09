import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:orgbots/main.dart';
import 'package:orgbots/src/api.dart';
import 'package:orgbots/src/bot3d/snapshots.dart';
import 'package:orgbots/src/screens/chat.dart';
import 'package:orgbots/src/screens/home.dart';
import 'package:orgbots/src/session.dart';

import 'support.dart';

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

  testWidgets('a lost connection says so in the chat, and clears when it is back', (tester) async {
    final (_, server) = await pump(tester, ChatScreen(bot: Bot.fromJson(bots.first)));
    const banner = 'Can’t reach the server — reconnecting…';
    expect(find.text(banner), findsNothing);

    server.failMessages = true;
    await tester.pump(const Duration(seconds: 4));
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text(banner), findsOneWidget);
    expect(find.text('Find the cheapest travel book'), findsOneWidget);

    server.failMessages = false;
    await tester.pump(const Duration(seconds: 4));
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text(banner), findsNothing);
  });
}
