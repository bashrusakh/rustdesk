use arboard::Clipboard;

fn main() {
	let mut ctx = Clipboard::new().unwrap();

	let img = ctx.get_image().unwrap();

	match img {
		arboard::ImageData::Rgba(img) => {
			println!("Image width is: {}", img.width);
			println!("Image height is: {}", img.height);
			println!("Image data is:\n{:?}", img.bytes);
		}
		arboard::ImageData::Png(png) => {
			println!("PNG data is:\n{:?}", png);
		}
		arboard::ImageData::Svg(svg) => {
			println!("SVG data is:\n{}", svg);
		}
	}
}
