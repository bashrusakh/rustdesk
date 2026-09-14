use super::parse_dconf;
use crate::{run, Mode, Result};

#[inline]
pub fn is_compliant(desktop: &str) -> bool {
    desktop.contains("GNOME") || desktop == "Unity" || desktop == "Pantheon"
}

pub fn get() -> Result<String> {
    parse_dconf(
        "gsettings",
        &["get", "org.gnome.desktop.background", "picture-uri"],
    )
}

pub fn set(path: &str) -> Result<()> {
    let uri = enquote::enquote('"', &format!("file://{}", path));
    run(
        "gsettings",
        &["set", "org.gnome.desktop.background", "picture-uri", &uri],
    )
}

pub fn get_dark() -> Result<String> {
    parse_dconf(
        "gsettings",
        &["get", "org.gnome.desktop.background", "picture-uri-dark"],
    )
}


pub fn set_dark(path: &str) -> Result<()> {
    let uri = enquote::enquote('"', &format!("file://{}", path));
    run(
        "gsettings",
        &["set", "org.gnome.desktop.background", "picture-uri-dark", &uri],
    )
}

pub fn set_mode(mode: Mode) -> Result<()> {
    run(
        "gsettings",
        &[
            "set",
            "org.gnome.desktop.background",
            "picture-options",
            &mode.get_gnome_string(),
        ],
    )
}

impl Mode {
    pub(crate) fn get_gnome_string(self) -> String {
        enquote::enquote(
            '"',
            match self {
                Mode::Center => "centered",
                Mode::Crop => "zoom",
                Mode::Fit => "scaled",
                Mode::Span => "spanned",
                Mode::Stretch => "stretched",
                Mode::Tile => "wallpaper",
            },
        )
    }
}
