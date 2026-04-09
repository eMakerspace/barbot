// Hook into the specific taxonomies your API uses
add_action( 'woocommerce_rest_insert_pa_mixers', 'update_acf_term_meta_via_api', 10, 3 );
add_action( 'woocommerce_rest_insert_pa_spirits', 'update_acf_term_meta_via_api', 10, 3 );

function update_acf_term_meta_via_api( $term, $request, $creating ) {
    
    // If your Python script sends 'viscosity', save it to the database
    if ( isset( $request['viscosity'] ) ) {
        $viscosity = sanitize_text_field( $request['viscosity'] );
        update_term_meta( $term->term_id, 'viscosity', $viscosity );
    }
}