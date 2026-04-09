// 1. Create a custom API endpoint for the machine to ping
add_action( 'rest_api_init', function () {
    register_rest_route( 'barmachine/v1', '/ping', array(
        'methods' => 'POST',
        'callback' => 'update_machine_heartbeat',
        'permission_callback' => '__return_true' 
    ) );
} );

function update_machine_heartbeat( WP_REST_Request $request ) {
    // Basic security token to prevent random people from pinging the endpoint
    $secret = $request->get_param( 'secret' );
    if ( $secret !== 'C5CE8A3C7FF82CC9F22E0B958BA247D5' ) {
        return new WP_Error( 'unauthorized', 'Invalid token', array( 'status' => 401 ) );
    }
    
    // Save the current Unix timestamp to the database
    update_option( 'machine_last_active', time() );
    return rest_ensure_response( array( 'status' => 'online' ) );
}

// 2. Lock the cart and checkout if the heartbeat is missing
add_action( 'woocommerce_check_cart_items', 'enforce_machine_heartbeat' );
add_action( 'woocommerce_checkout_process', 'enforce_machine_heartbeat' );

function enforce_machine_heartbeat() {
    $last_active = get_option( 'machine_last_active', 0 );
    
    // Set your timeout window (e.g., 60 seconds)
    $timeout_seconds = 60; 

    // If the current time minus the last ping is greater than the timeout, lock it down
    if ( ( time() - $last_active ) > $timeout_seconds ) {
        wc_add_notice( '<strong>Achtung:</strong> Der Barbot ist zurzeit offline oder in Vorbereitung. Es können keine Käufe getätigt werden. Bitte wenden Sie sich an einen Bartender.', 'error' );
    }
}