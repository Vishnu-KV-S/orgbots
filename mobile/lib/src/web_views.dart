import 'package:flutter/foundation.dart';

/// Whether this build can embed native web views — the sign-in page and the bot's
/// live screen. Not in the browser build (which links out instead), and switched off
/// by the widget tests, which have no platform to create one on.
class WebViews {
  static bool enabled = !kIsWeb;
}
