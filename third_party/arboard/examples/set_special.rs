use arboard::Clipboard;
use std::{
	sync::{Arc, Mutex},
	thread,
	time::Duration,
};

fn set(ctx: Arc<Mutex<Clipboard>>) {
	let mut ctx = ctx.lock().unwrap();

	let special_format = "dyn.arboard.pecial.format";

	ctx.set_special(special_format, &[1]).unwrap();
}

fn get(ctx: Arc<Mutex<Clipboard>>) {
	let mut ctx = ctx.lock().unwrap();

	let special_format = "dyn.arboard.pecial.format";
	println!("special format data: {:?}", ctx.get_special(special_format).unwrap());
}

fn main() {
	env_logger::init();

	let ctx = Arc::new(Mutex::new(Clipboard::new().unwrap()));
	let ctx2 = ctx.clone();
	thread::spawn(move || set(ctx2));

	thread::sleep(Duration::from_millis(1000));
	get(ctx.clone());
}
