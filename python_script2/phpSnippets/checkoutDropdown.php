

/**
 * Snippet Name:   WooCommerce Checkout How Did You Hear About Us
 * Snippet Author: ecommercehints.com
 */

// Create the custom select field in the billing section of the checkout form
add_action( 'woocommerce_after_checkout_billing_form', 'ecommercehints_checkout_select_field' );
function ecommercehints_checkout_select_field($checkout) {
    woocommerce_form_field(
        'studiengang',
        array(
            'type'     => 'select',
            'required' => true, // Shows an asterisk if true (*)
            'label'    => 'Studiengang',
            'options'  => array(
                ''                 => 'Bitte auswählen...',
                'ET'    => 'Elektrotechnik',
                'I'    => 'Informatik',
                'M-I'     => 'Maschienentechnik Innovation',
                'EEU'   => 'Erneuerbare Energien und Umwelttechnik',
                'WING-F'     => 'Wirtschaftsingenieur / -informatik',
				'ETH'    => 'ETH',
                'HSG'    => 'HSG',
                'UZH-PH'     => 'Uni Zürich / PH',
                'Buezer'   => 'Büezer',
                'Soz-Ges'     => 'Sozial / Gesundheit / Psychologie',
				'ABLR'  => 'ABLR (Architektur, Bau, Landschaft, Raum)',
				'MSE'  => 'Master',
				
            )
        ),
        ( isset($_POST['studiengang']) ? $_POST['studiengang'] : '' )
    );
}

// Show an error message if field is not populated
add_action( 'woocommerce_checkout_process', 'ecommercehints_custom_checkout_select_field_validation' );
function ecommercehints_custom_checkout_select_field_validation() {
    if ( empty( $_POST['studiengang'] ) ) {
        wc_add_notice( 'Bitte wählen Sie Ihren Studiengang aus.', 'error' );
    }
}

// Save the custom field data as order meta
add_action( 'woocommerce_checkout_update_order_meta', 'ecommercehints_save_custom_checkout_select_field' );
function ecommercehints_save_custom_checkout_select_field( $order_id ){
    if ( !empty( $_POST['studiengang'] ) ) {
        update_post_meta( $order_id, 'studiengang', sanitize_text_field( $_POST['studiengang'] ) );
    }
}