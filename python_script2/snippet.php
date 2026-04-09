/**
 * Expose bottle_properties (viscosity, bottle_size) on WooCommerce REST API
 * responses for pa_spirits and pa_mixers attribute terms.
 *
 * WooCommerce fires "woocommerce_rest_prepare_{taxonomy}" (not the generic
 * "woocommerce_rest_prepare_product_attribute_term") for each attribute term,
 * so we need one filter per taxonomy.
 */
function barbot_expose_bottle_properties( $response, $item, $request ) {
    $data = $response->get_data();

    $b_meta = get_term_meta( $item->term_id, 'bottle_size', true );
    $v_meta = get_term_meta( $item->term_id, 'viscosity',   true );

    $data['bottle_properties'] = array(
        'bottle_size' => ( is_numeric( $b_meta ) && $b_meta !== '' ) ? (float) $b_meta : 70.0,
        'viscosity'   => ( is_numeric( $v_meta ) && $v_meta !== '' ) ? (float) $v_meta :  1.0,
    );

    $response->set_data( $data );
    return $response;
}

add_filter( 'woocommerce_rest_prepare_pa_spirits', 'barbot_expose_bottle_properties', 10, 3 );
add_filter( 'woocommerce_rest_prepare_pa_mixers',  'barbot_expose_bottle_properties', 10, 3 );
