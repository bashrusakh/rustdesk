use super::Command;
use crate::{traits::*, GenericError};

impl Command {
    fn execute_core<C>(&self, ctx: &mut C) -> Result<bool, GenericError<C::PlatformError>>
    where
        C: FallibleContext + KeyboardContext + MouseContext + AsciiKeyboardContext,
    {
        use Command::*;
        match self {
            KeyDown(key) => ctx.key_down(*key),
            KeyUp(key) => ctx.key_up(*key),
            KeyClick(key) => ctx.key_click(*key),
            MouseMoveRel(dx, dy) => ctx.mouse_move_rel(*dx, *dy),
            MouseMoveAbs(x, y) => ctx.mouse_move_abs(*x, *y),
            MouseScroll(dx, dy) => ctx.mouse_scroll(*dx, *dy),
            MouseDown(button) => ctx.mouse_down(*button),
            MouseUp(button) => ctx.mouse_up(*button),
            MouseClick(button) => ctx.mouse_click(*button),
            AsciiCharDown(ch) => ctx.ascii_char_down(*ch),
            AsciiCharUp(ch) => ctx.ascii_char_up(*ch),
            AsciiChar(ch) => ctx.ascii_char(*ch),
            AsciiString(s) => ctx.ascii_string(s.as_slice()),
            _ => return Ok(false),
        }?;
        Ok(true)
    }
}
