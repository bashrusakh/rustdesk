use arboard::{Clipboard, ImageData};

fn main() {
	let mut ctx = Clipboard::new().unwrap();

	let svg = r#"<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!-- Created with Inkscape (http://www.inkscape.org/) -->

<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <circle cx="50" cy="50" r="40" stroke="black" stroke-width="2" fill="red" />
</svg>"#;

	let img_data = ImageData::svg(svg);
	ctx.set_image(img_data).unwrap();
}
