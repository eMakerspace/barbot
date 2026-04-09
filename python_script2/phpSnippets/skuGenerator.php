add_action( 'woocommerce_new_product', 'auto_assign_unique_sku', 10, 1 );
add_action( 'woocommerce_new_product_variation', 'auto_assign_unique_sku', 10, 1 );

function auto_assign_unique_sku( $product_id ) {
    // Get the product or variation object
    $product = wc_get_product( $product_id );
    
    // Check if it exists and if the SKU is currently empty
    if ( $product && empty( $product->get_sku() ) ) {
        
        // Generate the unique SKU using a prefix and the exact Product ID
        $unique_sku = 'DRINK-' . $product_id;
        
        // Set and save the new SKU
        $product->set_sku( $unique_sku );
        $product->save();
    }
}