use super::Error;
use super::MyCGEventSource;
use core_graphics::{display::CGDisplay, event::CGEvent};

impl crate::ScreenContext for super::Context {
    fn cursor_location(&self) -> Result<(i32, i32), Error> {
        let MyCGEventSource(event_source) = &*self.event_source;
        let event = match CGEvent::new(event_source.clone()) {
            Ok(e) => e,
            Err(()) => return Err(Error::Unknown),
        };
        let loc = event.location();
        Ok((loc.x as i32, loc.y as i32))
    }

    fn screen_size(&self) -> Result<(i32, i32), Error> {
        let display = CGDisplay::main();
        Ok((display.pixels_wide() as i32, display.pixels_high() as i32))
    }
}
