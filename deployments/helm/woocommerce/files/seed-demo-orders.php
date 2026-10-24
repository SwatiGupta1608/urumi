<?php
/**
 * Seed synthetic demo orders for exercising the MCP order tools.
 * Opt-in (init.demoData.enabled). Idempotent: created order IDs are kept in
 * the `urumi_demo_orders` option and only missing ones are re-created.
 *
 * Orders use a fake customer (example.invalid) and no payment gateway.
 * "status-test" is the single order reserved for status-change tests.
 *
 * Run with: wp eval-file seed-demo-orders.php
 */

if ( ! function_exists( 'wc_create_order' ) ) {
	WP_CLI::error( 'WooCommerce is not active.' );
}

$sku        = 'urumi-demo-item';
$product_id = wc_get_product_id_by_sku( $sku );
if ( ! $product_id ) {
	$product = new WC_Product_Simple();
	$product->set_name( 'Urumi demo item' );
	$product->set_sku( $sku );
	$product->set_regular_price( '6.00' );
	$product->set_status( 'publish' );
	$product_id = $product->save();
	WP_CLI::log( "created demo product {$product_id}" );
}
$product = wc_get_product( $product_id );

$wanted = array(
	'demo-processing' => array( 'status' => 'processing', 'qty' => 2 ),
	'demo-completed'  => array( 'status' => 'completed', 'qty' => 1 ),
	'status-test'     => array( 'status' => 'pending', 'qty' => 1 ),
);

$ids = get_option( 'urumi_demo_orders', array() );
if ( ! is_array( $ids ) ) {
	$ids = array();
}

foreach ( $wanted as $label => $spec ) {
	if ( ! empty( $ids[ $label ] ) && wc_get_order( $ids[ $label ] ) ) {
		continue;
	}
	$order = wc_create_order( array( 'created_via' => 'urumi-demo-seed' ) );
	if ( is_wp_error( $order ) ) {
		WP_CLI::error( "could not create {$label}: " . $order->get_error_message() );
	}
	$order->add_product( $product, $spec['qty'] );
	$order->set_billing_first_name( 'Demo' );
	$order->set_billing_last_name( ucfirst( str_replace( '-', ' ', $label ) ) );
	$order->set_billing_email( "{$label}@example.invalid" );
	$order->set_payment_method_title( 'Demo (no payment)' );
	$order->update_meta_data( '_urumi_demo_order', $label );
	$order->calculate_totals( false );
	$order->set_status( $spec['status'], 'Synthetic demo order.' );
	$order->save();
	$ids[ $label ] = $order->get_id();
	WP_CLI::log( "created {$label} order {$order->get_id()} ({$spec['status']})" );
}

update_option( 'urumi_demo_orders', $ids, false );
WP_CLI::success( 'demo orders: ' . wp_json_encode( $ids ) );
