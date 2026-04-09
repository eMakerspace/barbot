add_action( 'woocommerce_thankyou', 'display_pickup_code_on_thankyou_page', 5 );

function display_pickup_code_on_thankyou_page( $order_id ) {
    if ( ! $order_id ) {
        return;
    }

    // 1. Calculate the 2-digit code using modulo math
    $short_code = $order_id % 100;
    
    // 2. Format it to always show 2 digits (e.g., turns "7" into "07")
    $formatted_code = sprintf( "%02d", $short_code );

    // 3. Output the big visual box (HTML + Inline CSS)
echo '<div style="
    background: #ffffff; /* clean white background */
    border: 2px solid #4a90e2; /* subtle solid blue border */
    padding: 50px 30px; 
    text-align: center; 
    margin-bottom: 40px; 
    max-width: 600px;
    margin-left: auto;
    margin-right: auto;
">';
    
    echo '<h2 style="
        margin: 0 0 15px 0; 
        font-size: 32px; 
        color: #2c3e50; 
        text-transform: uppercase; 
        letter-spacing: 2px; 
        font-weight: 700;
        font-family: inherit;
    ">Pickup Code</h2>';
    
    // The giant number
    echo '<div style="
        font-size: 96px; 
        font-weight: 900; 
        color: #4a90e2; /* brand blue */
        line-height: 1; 
        margin: 0 0 20px 0;
        font-family: inherit;
        letter-spacing: 8px;
        user-select: all; /* so users can easily copy */
    ">' . esc_html($formatted_code) . '</div>';
    
    echo '<p style="
        margin: 0; 
        font-size: 20px; 
        color: #34495e; 
        font-weight: 600;
        font-family: inherit;
    ">Zeigen Sie diesen Screen, um die Bestellung abzuholen.</p>';
    
echo '</div>';
}