import 'package:flutter/material.dart';

import '../api.dart';

Color hexColor(Object? hex, [Color fallback = const Color(0xFFECEBE7)]) {
  if (hex is! String) return fallback;
  final v = int.tryParse(hex.replaceFirst('#', ''), radix: 16);
  return v == null ? fallback : Color(0xFF000000 | v);
}

/// A flat face in the bot's colours — the web app's `FlatFace`: shown while the 3D
/// bot loads, and in its place where WebGL is unavailable.
class FlatFace extends StatelessWidget {
  const FlatFace({super.key, required this.look, required this.size});

  final Json look;
  final double size;

  @override
  Widget build(BuildContext context) {
    final shape = look['shape'];
    final radius = shape == 'orb'
        ? size / 2
        : shape == 'capsule'
        ? size * 0.4
        : size * 0.24;
    final glow = hexColor(look['glow'], const Color(0xFFF5A623));
    final eyeKind = look['eyes'];
    final eyeH = eyeKind == 'visor' ? size * 0.09 : (eyeKind == 'dot' ? size * 0.1 : size * 0.18);
    final eyeW = eyeKind == 'visor' ? size * 0.4 : (eyeKind == 'pill' ? size * 0.1 : eyeH);
    Widget eye() => Container(
      width: eyeW,
      height: eyeH,
      decoration: BoxDecoration(
        color: glow,
        borderRadius: BorderRadius.circular(eyeKind == 'square' ? eyeH * 0.2 : eyeH),
        boxShadow: [BoxShadow(color: glow.withValues(alpha: 0.8), blurRadius: size * 0.08)],
      ),
    );
    return SizedBox.square(
      dimension: size,
      child: Center(
        child: Container(
          width: size * 0.9,
          height: size * 0.84,
          decoration: BoxDecoration(
            color: hexColor(look['body']),
            borderRadius: BorderRadius.circular(radius),
          ),
          alignment: Alignment.center,
          child: Container(
            width: size * 0.66,
            height: size * 0.4,
            decoration: BoxDecoration(
              color: const Color(0xFF111215),
              borderRadius: BorderRadius.circular(size * 0.2),
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              spacing: size * 0.1,
              children: [eye(), if (eyeKind != 'visor') eye()],
            ),
          ),
        ),
      ),
    );
  }
}
