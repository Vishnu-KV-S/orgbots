import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';

void tap() => HapticFeedback.selectionClick();

enum ButtonKind { primary, secondary, ghost, danger }

/// The app's one button: a pill, flat, with a spinner while [busy].
class PillButton extends StatelessWidget {
  const PillButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.kind = ButtonKind.primary,
    this.icon,
    this.busy = false,
    this.expand = true,
  });

  final String label;
  final VoidCallback? onPressed;
  final ButtonKind kind;
  final IconData? icon;
  final bool busy;
  final bool expand;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final bg = switch (kind) {
      ButtonKind.primary => p.accent,
      ButtonKind.ghost => Colors.transparent,
      _ => p.raised,
    };
    final fg = switch (kind) {
      ButtonKind.primary => p.onAccent,
      ButtonKind.danger => p.danger,
      _ => p.text,
    };
    final enabled = onPressed != null && !busy;
    final child = busy
        ? SizedBox.square(
            dimension: 20,
            child: CircularProgressIndicator(strokeWidth: 2, color: fg),
          )
        : Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[Icon(icon, size: 19, color: fg), const SizedBox(width: 8)],
              Flexible(
                // Wraps to a second line before it would cut a label short (very
                // large text on a small phone).
                child: Text(
                  label,
                  maxLines: 2,
                  textAlign: TextAlign.center,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: fg, fontSize: 16, fontWeight: FontWeight.w600),
                ),
              ),
            ],
          );
    return Opacity(
      opacity: onPressed == null ? 0.4 : 1,
      child: Material(
        color: bg,
        shape: StadiumBorder(
          side: kind == ButtonKind.ghost ? BorderSide(color: p.lineStrong) : BorderSide.none,
        ),
        child: InkWell(
          customBorder: const StadiumBorder(),
          onTap: enabled
              ? () {
                  tap();
                  onPressed!();
                }
              : null,
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: 50, minWidth: expand ? double.infinity : 0),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 6),
              child: Center(widthFactor: 1, child: child),
            ),
          ),
        ),
      ),
    );
  }
}

/// A round icon button, filled when [filled].
class RoundIcon extends StatelessWidget {
  const RoundIcon({
    super.key,
    required this.icon,
    required this.onPressed,
    required this.tooltip,
    this.filled = false,
    this.size = 40,
  });

  final IconData icon;
  final VoidCallback onPressed;
  final String tooltip;
  final bool filled;
  final double size;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    // The circle is [size]; the target around it is at least 48 points, as both
    // platforms' guidelines ask.
    return Tooltip(
      message: tooltip,
      child: SizedBox.square(
        dimension: size < 48 ? 48 : size,
        child: Center(
          child: Material(
            color: filled ? p.raised : Colors.transparent,
            shape: const CircleBorder(),
            child: InkWell(
              customBorder: const CircleBorder(),
              onTap: () {
                tap();
                onPressed();
              },
              child: SizedBox.square(
                dimension: size,
                child: Icon(icon, size: size * 0.55, color: p.text),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class Notice extends StatelessWidget {
  const Notice(this.text, {super.key, this.error = true});
  final String text;
  final bool error;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: p.raised, borderRadius: BorderRadius.circular(Radii.md)),
      child: Text(
        text,
        style: TextStyle(
          color: error ? p.danger : p.textDim,
          fontSize: 14,
          fontWeight: FontWeight.w500,
          height: 1.4,
        ),
      ),
    );
  }
}

/// A small uppercase section heading.
class SectionLabel extends StatelessWidget {
  const SectionLabel(this.text, {super.key});
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 14, bottom: 8, left: 4),
    child: Text(
      text.toUpperCase(),
      style: TextStyle(
        color: Palette.of(context).textFaint,
        fontSize: 12.5,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.7,
      ),
    ),
  );
}

InputDecoration fieldDecoration(BuildContext context, {String? hint, bool pill = false}) {
  final p = Palette.of(context);
  final border = OutlineInputBorder(
    borderRadius: BorderRadius.circular(pill ? Radii.pill : Radii.md),
    borderSide: BorderSide(color: p.line),
  );
  return InputDecoration(
    hintText: hint,
    hintStyle: TextStyle(color: p.textFaint),
    filled: true,
    fillColor: p.raised,
    contentPadding: EdgeInsets.symmetric(horizontal: pill ? 20 : 16, vertical: 15),
    border: border,
    enabledBorder: border,
    focusedBorder: border.copyWith(borderSide: BorderSide(color: p.lineStrong)),
  );
}

/// Keeps a column of content at a readable width on tablets and in landscape,
/// centred, the way a chat app does; on a phone it changes nothing.
class Readable extends StatelessWidget {
  const Readable({super.key, required this.child, this.maxWidth = 720});
  final Widget child;
  final double maxWidth;

  @override
  Widget build(BuildContext context) => Align(
    alignment: Alignment.topCenter,
    child: ConstrainedBox(
      constraints: BoxConstraints(maxWidth: maxWidth),
      child: child,
    ),
  );
}
