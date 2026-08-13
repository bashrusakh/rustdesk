use tfc::{traits::*, Context, Key};

#[test]
fn test_char() -> anyhow::Result<()> {
    std::env::set_var("DISPLAY", ":0");

    let mut kbd = Context::new()?;
    kbd.key_down(Key::Shift)?;
    kbd.unicode_char_down('q')?;
    kbd.unicode_char_up('q')?;
    kbd.key_up(Key::Shift)?;

    Ok(())
}

#[test]
fn test_dead_char() -> anyhow::Result<()> {
    std::env::set_var("DISPLAY", ":0");

    let mut kbd = Context::new()?;
    kbd.unicode_char_down('â')?;
    kbd.unicode_char_up('â')?;

    kbd.unicode_char_down('ù')?;
    kbd.unicode_char_up('ù')?;

    Ok(())
}
