use arboard::Clipboard;

fn main() {
	env_logger::init();
	let mut ctx = Clipboard::new().unwrap();

	let special_format = "dyn.arboard.pecial.format";

    println!("{:?}", ctx.get_special(special_format).unwrap());
}
