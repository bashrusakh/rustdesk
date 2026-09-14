enumeration!(
    Key,
    "A keyboard key used by the [`KeyboardContext`](crate::KeyboardContext) trait.",
    [
        // Modifier keys
        (CapsLock, "Caps Lock"), // CapsLock
        (Shift, "Shift"),        // ShiftLeft
        (Control, "Control"),    // ControlLeft
        (Alt, "Alt"),            // Alt
        (Meta, "Meta"),          // MetaLeft. Command on macOS. Windows key on Windows.
        // repeat (ControlOrMeta, "Control or Meta"),     // ControlLeft. Command on macOS. Control on Windows.
        (RightShift, "Right Shift"),     // ShiftRight
        (RightControl, "Right Control"), // ControlRight
        (RightAlt, "Right Alt"),         // AltGr
        (RightMeta, "Right Meta"),       // MetaRight. Command on macOS. Windows key on Windows.
        // repeat (RightControlOrMeta, "Right Control or Meta"),  //ControlRight. Command on macOS. Control on Windows.
        (Fn, "Fn"),
        // Controls and symbols
        (ReturnOrEnter, "Return or Enter"),         // Return
        (Escape, "Escape"),                         // Escape
        (DeleteOrBackspace, "Delete or Backspace"), // Backspace
        (ForwardDelete, "Forward Delete"),          // Delete
        (Tab, "Tab"),                               // Tab
        (Space, "Space"),                           // Space
        (Minus, "Minus"),                           // Minus
        (Equal, "Equal"),                           // Equal
        (LeftBracket, "Left Bracket"),              // LeftBracket
        (RightBracket, "Right Bracket"),            // RightBracket
        (Backslash, "Backslash"),                   // BackSlash
        (Semicolon, "Semicolon"),                   // SemiColon
        (Quote, "Quote"),                           // Quote
        (Grave, "Grave"),                           // BackQuote. Key_Grave
        (Comma, "Comma"),                           // Comma
        (Period, "Period"),                         // Dot
        (Slash, "Slash"),                           // Slash
        (IntlBackslash, "IntlBackslash"),           //  IntlBackslash
        (Apps, "Apps"),                             // Apps
        // Arrow keys
        (UpArrow, "Up Arrow"),        // UpArrow
        (RightArrow, "Right Arrow"),  // RightArrow
        (DownArrow, "Down Arrow"),    // DownArrow
        (LeftArrow, "Left Arrow"),    // LeftArrow
        (PageUp, "Page Up"),          // PageUp
        (PageDown, "Page Down"),      // PageDown
        (Home, "Home"),               // Home
        (End, "End"),                 // End
        (Insert, "Insert"),           // Insert
        (PrintScreen, "PrintScreen"), // PrintScreen
        (Print, "Print"),             // Print
        (ScrollLock, "ScrollLock"),   // ScrollLock
        (Pause, "Pause"),             // Pause
        (NumLock, "Num Lock"),        // NumLock
        // Letter keys
        (A, "A"), // KeyA
        (B, "B"), // KeyB
        (C, "C"), // KeyC
        (D, "D"), // KeyD
        (E, "E"), // KeyE
        (F, "F"), // KeyF
        (G, "G"), // KeyG
        (H, "H"), // KeyH
        (I, "I"), // KeyI
        (J, "J"), // KeyJ
        (K, "K"), // KeyK
        (L, "L"), // KeyL
        (M, "M"), // KeyM
        (N, "N"), // KeyN
        (O, "O"), // KeyO
        (P, "P"), // KeyP
        (Q, "Q"), // KeyQ
        (R, "R"), // KeyR
        (S, "S"), // KeyS
        (T, "T"), // KeyT
        (U, "U"), // KeyU
        (V, "V"), // KeyV
        (W, "W"), // KeyW
        (X, "X"), // KeyX
        (Y, "Y"), // KeyY
        (Z, "Z"), // KeyZ
        // Number keys
        (N0, "0"), // Num1
        (N1, "1"), // Num2
        (N2, "2"), // Num3
        (N3, "3"), // Num4
        (N4, "4"), // Num5
        (N5, "5"), // Num6
        (N6, "6"), // Num7
        (N7, "7"), // Num8
        (N8, "8"), // Num9
        (N9, "9"), // Num10
        // Number pad numbers
        (Numpad0, "Numpad 0"), // Kp0
        (Numpad1, "Numpad 1"), // Kp1
        (Numpad2, "Numpad 2"), // Kp2
        (Numpad3, "Numpad 3"), // Kp3
        (Numpad4, "Numpad 4"), // Kp4
        (Numpad5, "Numpad 5"), // Kp5
        (Numpad6, "Numpad 6"), // Kp6
        (Numpad7, "Numpad 7"), // Kp7
        (Numpad8, "Numpad 8"), // Kp8
        (Numpad9, "Numpad 9"), // Kp9
        // Number pad keys
        (NumpadClear, "Numpad Clear"),
        (NumpadEquals, "Numpad Equals"),
        (NumpadDivide, "Numpad Divide"),     // KpDivide
        (NumpadMultiply, "Numpad Multiply"), // KpMultiply
        (NumpadMinus, "Numpad Minus"),       // KpMinus
        (NumpadPlus, "Numpad Plus"),         // KpPlus
        (NumpadEnter, "Numpad Enter"),       // KpReturn
        (NumpadDecimal, "Numpad Decimal"),   // KpDecimal
        // Function keys
        (F1, "F1"),   // F1
        (F2, "F2"),   // F2
        (F3, "F3"),   // F3
        (F4, "F4"),   // F4
        (F5, "F5"),   // F5
        (F6, "F6"),   // F6
        (F7, "F7"),   // F7
        (F8, "F8"),   // F8
        (F9, "F9"),   // F9
        (F10, "F10"), // F10
        (F11, "F11"), // F11
        (F12, "F12"), // F12
        // Media controls
        (FastForward, "Fast-Forward"),
        (Rewind, "Rewind"),
        (PlayPause, "Play/Pause"),
        (VolumeUp, "Volume Up"),
        (VolumeDown, "Volume Down"),
        (Mute, "Mute"),
    ]
);
