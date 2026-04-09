add_filter( 'woocommerce_ajax_variation_threshold', 'custom_wc_ajax_variation_threshold', 10, 2 );
function custom_wc_ajax_variation_threshold( $qty, $product ) {
    return 200; // Set this number slightly higher than your maximum possible combinations
}