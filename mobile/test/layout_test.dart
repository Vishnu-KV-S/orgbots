import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:orgbots/src/api.dart';
import 'package:orgbots/src/screens/chat.dart';
import 'package:orgbots/src/screens/connect.dart';
import 'package:orgbots/src/screens/home.dart';
import 'package:orgbots/src/screens/live_screen.dart';
import 'package:orgbots/src/screens/new_bot.dart';
import 'package:orgbots/src/screens/settings.dart';
import 'package:orgbots/src/screens/signin.dart';
import 'package:orgbots/src/theme.dart';
import 'package:orgbots/src/widgets/common.dart';

import 'support.dart';

/// Every screen, at a small phone, a regular phone and a tablet, at normal and large
/// text, in both themes, with data built to break layouts (`awkwardBot`). Flutter
/// reports any overflow or layout error as an exception, which fails the test.
const sizes = {'small phone': Size(320, 568), 'phone': Size(390, 844), 'tablet': Size(1024, 1366)};
const scales = [1.0, 1.4, 2.0];

/// Screens whose interesting state comes from the server rather than the widget.
final servers = <String, FakeServer Function()>{
  'home with no bots yet': () => FakeServer(noBots: true),
  'home with the server down': () => FakeServer(down: true),
};

final screens = <String, Widget Function()>{
  'home with no bots yet': () => const HomeScreen(),
  'home with the server down': () => const HomeScreen(),
  'home': () => const HomeScreen(),
  'chat': () => ChatScreen(bot: Bot.fromJson(bots.first)),
  'chat with awkward content': () => ChatScreen(bot: Bot.fromJson(awkwardBot)),
  'empty chat': () => ChatScreen(bot: Bot.fromJson(bots[1])),
  'new bot': () => const NewBotScreen(),
  'settings': () => const SettingsScreen(),
  'connect': () => const ConnectScreen(),
  'sign in': () => const SignInScreen(),
  'live screen': () => const LiveScreen(botId: 'b3', botName: longName),
};

void main() {
  setUpAll(loadRealFonts);

  for (final MapEntry(key: screen, value: build) in screens.entries) {
    for (final MapEntry(key: sizeName, value: size) in sizes.entries) {
      for (final scale in scales) {
        for (final palette in [Palette.dark, Palette.light]) {
          final theme = palette == Palette.dark ? 'dark' : 'light';
          testWidgets('$screen · $sizeName · text ×$scale · $theme', (tester) async {
            await pumpAt(
              tester,
              build(),
              size: size,
              textScale: scale,
              palette: palette,
              server: servers[screen]?.call(),
            );
            for (var i = 0; i < 5; i++) {
              await tester.pump(const Duration(milliseconds: 100));
            }
            expect(tester.takeException(), isNull);
            // A button's label is never cut short with an ellipsis.
            for (final button in find.byType(PillButton).evaluate()) {
              final label = (button.widget as PillButton).label;
              for (final text
                  in find
                      .descendant(of: find.byWidget(button.widget), matching: find.byType(RichText))
                      .evaluate()) {
                final paragraph = text.renderObject! as RenderParagraph;
                expect(paragraph.didExceedMaxLines, isFalse, reason: '"$label" is truncated');
              }
            }
          });
        }
      }
    }
  }

  // Flutter's accessibility guidelines, on a regular phone in both themes: tap
  // targets big enough to hit, every tap target labelled for screen readers, and text
  // with enough contrast to read.
  for (final MapEntry(key: screen, value: build) in screens.entries) {
    for (final palette in [Palette.dark, Palette.light]) {
      final theme = palette == Palette.dark ? 'dark' : 'light';
      testWidgets('$screen · accessibility · $theme', (tester) async {
        final semantics = tester.ensureSemantics();
        await pumpAt(tester, build(), palette: palette, server: servers[screen]?.call());
        for (var i = 0; i < 5; i++) {
          await tester.pump(const Duration(milliseconds: 100));
        }
        await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
        await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
        await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
        await expectLater(tester, meetsGuideline(textContrastGuideline));
        semantics.dispose();
      });
    }
  }
}
