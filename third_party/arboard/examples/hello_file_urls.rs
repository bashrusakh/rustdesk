use arboard::{Clipboard, ClipboardData, ClipboardFormat};

fn main() {
	env_logger::init();
	let mut clipboard = Clipboard::new().unwrap();
	println!(
		"Clipboard urls was: {:?}",
		clipboard.get_formats(&vec![ClipboardFormat::FileUrl]).unwrap()
	);

	let urls = vec!["/tmp/test1.txt".to_owned(), "/tmp/test2.txt".to_owned()];
	clipboard.set_formats(&vec![ClipboardData::FileUrl(urls.clone())]).unwrap();
	println!("But now the clipboard urls should be: \"{}\"", urls.join("\n"));

	println!(
		"Clipboard urls is: {:?}",
		clipboard.get_formats(&vec![ClipboardFormat::FileUrl]).unwrap()
	);
}
