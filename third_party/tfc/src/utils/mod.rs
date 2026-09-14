mod non_zero;

#[allow(unused)]
pub use non_zero::*;

#[inline]
pub fn is_aligned<T>(ptr: *const T) -> bool {
    let align = std::mem::align_of::<T>();
    (ptr as usize) & (align - 1) == 0
}
