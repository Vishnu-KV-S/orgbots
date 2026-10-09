import 'package:flutter/material.dart';

/// Design tokens, mirroring `ui/styles/tokens.css`: monochrome and flat, ranked by
/// brightness alone. The only colour on screen is the bots themselves. The dark
/// theme is true black, the way an OLED phone wants it; light is the same ranking
/// inverted.
@immutable
class Palette extends ThemeExtension<Palette> {
  const Palette({
    required this.bg,
    required this.surface,
    required this.raised,
    required this.pressed,
    required this.line,
    required this.lineStrong,
    required this.text,
    required this.textDim,
    required this.textFaint,
    required this.accent,
    required this.onAccent,
    required this.bubble,
    required this.danger,
    required this.brightness,
  });

  final Color bg;
  final Color surface;
  final Color raised;
  final Color pressed;
  final Color line;
  final Color lineStrong;
  final Color text;
  final Color textDim;
  final Color textFaint;

  /// Emphasis — the send button, a live card's border: near-white on dark.
  final Color accent;
  final Color onAccent;
  final Color bubble;
  final Color danger;
  final Brightness brightness;

  static const dark = Palette(
    bg: Color(0xFF000000),
    surface: Color(0xFF0C0C0C),
    raised: Color(0xFF161616),
    pressed: Color(0xFF222222),
    line: Color(0xFF1F1F1F),
    lineStrong: Color(0xFF2E2E2E),
    text: Color(0xFFF1F1F1),
    textDim: Color(0xFFA3A3A3),
    textFaint: Color(0xFF6B6B6B),
    accent: Color(0xFFF1F1F1),
    onAccent: Color(0xFF000000),
    bubble: Color(0xFF1C1C1C),
    danger: Color(0xFFFF6B5E),
    brightness: Brightness.dark,
  );

  static const light = Palette(
    bg: Color(0xFFFFFFFF),
    surface: Color(0xFFFAFAFA),
    raised: Color(0xFFF2F2F2),
    pressed: Color(0xFFE6E6E6),
    line: Color(0xFFECECEC),
    lineStrong: Color(0xFFD9D9D9),
    text: Color(0xFF0D0D0D),
    textDim: Color(0xFF5C5C5C),
    textFaint: Color(0xFF9A9A9A),
    accent: Color(0xFF0D0D0D),
    onAccent: Color(0xFFFFFFFF),
    bubble: Color(0xFFF1F1F1),
    danger: Color(0xFFD93A2B),
    brightness: Brightness.light,
  );

  static Palette of(BuildContext context) => Theme.of(context).extension<Palette>()!;

  @override
  Palette copyWith() => this;

  @override
  Palette lerp(Palette? other, double t) => t < 0.5 ? this : (other ?? this);
}

ThemeData themeFor(Palette p) {
  final base = ThemeData(
    brightness: p.brightness,
    useMaterial3: true,
    colorScheme: ColorScheme.fromSeed(
      seedColor: p.accent,
      brightness: p.brightness,
      surface: p.bg,
      primary: p.accent,
      onPrimary: p.onAccent,
      error: p.danger,
    ),
  );
  return base.copyWith(
    scaffoldBackgroundColor: p.bg,
    canvasColor: p.bg,
    extensions: [p],
    splashFactory: NoSplash.splashFactory,
    highlightColor: Colors.transparent,
    dividerColor: p.line,
    textTheme: base.textTheme.apply(bodyColor: p.text, displayColor: p.text),
    iconTheme: IconThemeData(color: p.text),
    appBarTheme: AppBarTheme(
      backgroundColor: p.bg,
      surfaceTintColor: Colors.transparent,
      foregroundColor: p.text,
      elevation: 0,
      scrolledUnderElevation: 0,
      centerTitle: true,
    ),
    bottomSheetTheme: BottomSheetThemeData(
      backgroundColor: p.surface,
      surfaceTintColor: Colors.transparent,
      showDragHandle: true,
      dragHandleColor: p.lineStrong,
    ),
    dialogTheme: DialogThemeData(backgroundColor: p.surface, surfaceTintColor: Colors.transparent),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: p.raised,
      contentTextStyle: TextStyle(color: p.text),
      behavior: SnackBarBehavior.floating,
    ),
    textSelectionTheme: TextSelectionThemeData(
      cursorColor: p.text,
      selectionColor: p.textFaint.withValues(alpha: 0.4),
      selectionHandleColor: p.text,
    ),
    switchTheme: SwitchThemeData(
      thumbColor: WidgetStatePropertyAll(p.onAccent),
      trackColor: WidgetStateProperty.resolveWith(
        (s) => s.contains(WidgetState.selected) ? p.accent : p.lineStrong,
      ),
      trackOutlineColor: const WidgetStatePropertyAll(Colors.transparent),
    ),
    progressIndicatorTheme: ProgressIndicatorThemeData(color: p.textDim),
  );
}

/// Radii, as on the web: small, standard, large, and pills.
class Radii {
  static const sm = 10.0;
  static const md = 14.0;
  static const lg = 22.0;
  static const pill = 999.0;
}
