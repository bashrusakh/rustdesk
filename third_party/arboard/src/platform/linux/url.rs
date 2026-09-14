use super::into_unknown;
use crate::Error;

// on x11, path will be encode as
// "/home/rustdesk/pictures/🖼️.png" -> "file:///home/rustdesk/pictures/%F0%9F%96%BC%EF%B8%8F.png"
// url encode and decode is needed
const ENCODE_SET: percent_encoding::AsciiSet = percent_encoding::CONTROLS.add(b' ').remove(b'/');

pub(super) fn encode_path_to_uri(path: &str) -> String {
	let encoded = percent_encoding::percent_encode(path.as_bytes(), &ENCODE_SET).to_string();
	format!("file://{}", encoded)
}

pub(super) fn parse_uri_to_path(encoded_uri: &str) -> Result<String, Error> {
	let encoded_path = encoded_uri.trim_start_matches("file://");
	let path_str = percent_encoding::percent_decode_str(encoded_path)
		.decode_utf8()
		.map_err(|e| into_unknown("failed to decode path", e))?;
	Ok(path_str.to_string())
}

// helper parse function
// convert 'text/uri-list' data to a list of valid Paths
// # Note
// - none utf8 data will lead to error
pub(super) fn parse_plain_uri_list(v: Vec<u8>) -> Result<Vec<String>, Error> {
	let text = String::from_utf8(v)
		.map_err(|e| into_unknown("failed to convert file urls to utf-8", e))?;
	parse_uri_list(&text)
}

// helper parse function
// convert 'text/uri-list' data to a list of valid Paths
// # Note
// - none utf8 data will lead to error
pub(super) fn parse_uri_list(text: &str) -> Result<Vec<String>, Error> {
	let mut list = Vec::new();

	for line in text.lines() {
		if !line.starts_with("file://") {
			continue;
		}
		let decoded = parse_uri_to_path(line)?;
		list.push(decoded)
	}
	Ok(list)
}

#[cfg(test)]
mod uri_test {
	#[test]
	fn test_conversion() {
		let path = "/home/rustdesk/pictures/🖼️.png";
		let uri = super::encode_path_to_uri(&path);
		assert_eq!(uri, "file:///home/rustdesk/pictures/%F0%9F%96%BC%EF%B8%8F.png");
		let convert_back = super::parse_uri_to_path(&uri).unwrap();
		assert_eq!(path, convert_back);
	}

	#[test]
	fn parse_list() {
		let uri_list = r#"file:///home/rustdesk/pictures/%F0%9F%96%BC%EF%B8%8F.png
file:///home/rustdesk/pictures/%F0%9F%96%BC%EF%B8%8F.png
"#;
		let list = super::parse_uri_list(uri_list.into()).unwrap();
		assert!(list.len() == 2);
		assert_eq!(list[0], list[1]);
	}
}
