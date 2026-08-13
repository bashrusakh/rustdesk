use arboard::{Clipboard, ClipboardData, ClipboardFormat};
use std::{
	sync::{Arc, Mutex},
	thread,
	time::Duration,
};

const FORMAT_SPECIAL: &str = "dyn.arboard.pecial.format";

fn set(ctx: Arc<Mutex<Clipboard>>, vec_data: Vec<(ClipboardFormat, ClipboardData)>) {
	let mut ctx = ctx.lock().unwrap();
	ctx.set_formats(&vec_data.into_iter().map(|(_, d)| d).collect::<Vec<_>>()).unwrap();
}

fn get(ctx: Arc<Mutex<Clipboard>>, vec_data: Vec<(ClipboardFormat, ClipboardData)>) {
	let mut ctx = ctx.lock().unwrap();
	let mut formats = Vec::new();
	let mut data = Vec::new();
	for (f, d) in vec_data.into_iter() {
		formats.push(f);
		data.push(d);
	}

	for d in ctx.get_formats(&formats).unwrap() {
		println!("data: {:?}", d);
	}
}

fn main() {
	env_logger::init();

	let vec_data = vec![
		(ClipboardFormat::Text, ClipboardData::Text("Hello, world!".to_string())),
		(ClipboardFormat::Html, ClipboardData::Html("<b>Hello, world!</b>".to_string())),
		(ClipboardFormat::Rtf, ClipboardData::Rtf("{\\rtf1\\ansi\\b Hello, world!}".to_string())),
		(
			ClipboardFormat::ImageRgba,
			ClipboardData::Image(arboard::ImageData::rgba(
				2,
				2,
				[255, 100, 100, 255, 100, 255, 100, 100, 100, 100, 255, 100, 0, 0, 0, 255]
					.as_ref()
					.into(),
			)),
		),
		(
			ClipboardFormat::ImagePng,
			ClipboardData::Image(arboard::ImageData::png(
				[
					137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 2, 0, 0,
					0, 2, 8, 6, 0, 0, 0, 114, 182, 13, 36, 0, 0, 0, 29, 73, 68, 65, 84, 120, 1, 1,
					18, 0, 237, 255, 0, 255, 100, 100, 255, 100, 255, 100, 100, 0, 100, 100, 255,
					100, 0, 0, 0, 255, 83, 20, 8, 28, 106, 36, 154, 137, 0, 0, 0, 0, 73, 69, 78,
					68, 174, 66, 96, 130,
				]
				.as_ref()
				.into(),
			)),
		),
		(
			ClipboardFormat::ImageSvg,
			ClipboardData::Image(arboard::ImageData::svg(
				r#"<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!-- Created with Inkscape (http://www.inkscape.org/) -->

<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <circle cx="50" cy="50" r="40" stroke="black" stroke-width="2" fill="red" />
</svg>"#,
			)),
		),
		(
			ClipboardFormat::Special(FORMAT_SPECIAL),
			ClipboardData::Special((FORMAT_SPECIAL.to_string(), vec![1])),
		),
	];

	let ctx = Arc::new(Mutex::new(Clipboard::new().unwrap()));
	let ctx2 = ctx.clone();
	let vec_data2 = vec_data.clone();
	thread::spawn(move || set(ctx2, vec_data2));

	thread::sleep(Duration::from_millis(1000));
	get(ctx.clone(), vec_data);
}
