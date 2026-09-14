// Create a virtual keyboard, just while this is running.
// Generally this requires root.

use evdev::{uinput::VirtualDeviceBuilder, AttributeSet, EventType, InputEvent, Key, LedType};
use std::thread::sleep;
use std::time::Duration;

fn main() -> std::io::Result<()> {
    let mut keys = AttributeSet::<evdev::Key>::new();
    for i in evdev::Key::KEY_ESC.code()..(evdev::Key::BTN_TRIGGER_HAPPY40.code() + 1) {
        let key = evdev::Key::new(i);
        if !format!("{:?}", &key).contains("unknown key") {
            println!("add {}, {:?}", i, evdev::Key::new(i));
            keys.insert(evdev::Key::new(i));
        }
    }

    let mut leds = AttributeSet::<evdev::LedType>::new();
    leds.insert(evdev::LedType::LED_CAPSL);
    leds.insert(evdev::LedType::LED_SCROLLL);

    let mut miscs = AttributeSet::<evdev::MiscType>::new();
    miscs.insert(evdev::MiscType::MSC_SCAN);

    let mut device = VirtualDeviceBuilder::new()?
        .name("Fake Keyboard")
        .with_keys(&keys)?
        .with_leds(&leds)?
        .with_miscs(&miscs)?
        .build()
        .unwrap();

    let type_ = EventType::KEY;
    let code_a = Key::KEY_A.code();

    println!("Waiting for Ctrl-C...");

    loop {
        let mut chosen = String::new();
        std::io::stdin().read_line(&mut chosen).unwrap();

        let capslock_down = InputEvent::new(type_, Key::KEY_CAPSLOCK.code(), 1);
        let capslock_up = InputEvent::new(type_, Key::KEY_CAPSLOCK.code(), 0);
        device.emit(&[capslock_down, capslock_up]).unwrap();
        sleep(Duration::from_millis(300));
        println!("leftctrl_down, get_key_state: {:?}", device.get_key_state());
        println!("leftctrl_down, get_led_state: {:?}", device.get_led_state());

        let key_a_down = InputEvent::new(type_, code_a, 1);
        let key_a_up = InputEvent::new(type_, code_a, 0);
        device.emit(&[key_a_down, key_a_up]).unwrap();
        sleep(Duration::from_millis(300));
        println!("key_a_down, get_key_state: {:?}", device.get_key_state());
        println!("key_a_down, get_led_state: {:?}", device.get_led_state());
    }
}
