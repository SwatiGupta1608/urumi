<?php
/**
 * Upsert a WooCommerce REST API key from values supplied in a Kubernetes Secret.
 *
 * WooCommerce has no WP-CLI command for creating REST keys, so this mirrors
 * WC_Auth::create_keys() (includes/class-wc-auth.php): the consumer key is
 * stored as wc_api_hash( $ck ), the secret in plain text, plus the last
 * seven characters of the key. Keys are matched by description, so rotating
 * the Secret and re-running the Job replaces the old key.
 *
 * Run with: wp eval-file upsert-api-key.php
 */

if ( ! function_exists( 'wc_api_hash' ) ) {
	WP_CLI::error( 'WooCommerce is not active.' );
}

$read_file = static function ( $env ) {
	$path = getenv( $env );
	if ( ! $path || ! is_readable( $path ) ) {
		WP_CLI::error( "{$env} is not set or not readable." );
	}
	return trim( file_get_contents( $path ) );
};

$consumer_key    = $read_file( 'WC_CONSUMER_KEY_FILE' );
$consumer_secret = $read_file( 'WC_CONSUMER_SECRET_FILE' );
$permissions     = getenv( 'WC_API_PERMISSIONS' );
$description     = getenv( 'WC_API_DESCRIPTION' );
$login           = getenv( 'WC_API_USER' );

// Same shape WooCommerce generates: prefix + bin2hex( random_bytes( 20 ) ).
if ( ! preg_match( '/^ck_[0-9a-f]{40}$/', $consumer_key ) ) {
	WP_CLI::error( 'consumer key must match ck_ followed by 40 lowercase hex characters.' );
}
if ( ! preg_match( '/^cs_[0-9a-f]{40}$/', $consumer_secret ) ) {
	WP_CLI::error( 'consumer secret must match cs_ followed by 40 lowercase hex characters.' );
}
if ( ! in_array( $permissions, array( 'read', 'write', 'read_write' ), true ) ) {
	WP_CLI::error( 'WC_API_PERMISSIONS must be read, write or read_write.' );
}
if ( ! $description ) {
	WP_CLI::error( 'WC_API_DESCRIPTION is required.' );
}

$user = get_user_by( 'login', $login );
if ( ! $user ) {
	WP_CLI::error( "User {$login} does not exist." );
}

global $wpdb;
$table = $wpdb->prefix . 'woocommerce_api_keys';
$hash  = wc_api_hash( $consumer_key );

$existing = $wpdb->get_row(
	$wpdb->prepare( "SELECT key_id, user_id, permissions, consumer_key, consumer_secret FROM {$table} WHERE description = %s", $description )
);

if ( $existing
	&& hash_equals( $existing->consumer_key, $hash )
	&& hash_equals( $existing->consumer_secret, $consumer_secret )
	&& (int) $existing->user_id === (int) $user->ID
	&& $existing->permissions === $permissions
) {
	WP_CLI::success( "API key '{$description}' is already up to date (key_id {$existing->key_id})." );
	return;
}

$wpdb->delete( $table, array( 'description' => $description ), array( '%s' ) );
$wpdb->delete( $table, array( 'consumer_key' => $hash ), array( '%s' ) );

$inserted = $wpdb->insert(
	$table,
	array(
		'user_id'         => $user->ID,
		'description'     => $description,
		'permissions'     => $permissions,
		'consumer_key'    => $hash,
		'consumer_secret' => $consumer_secret,
		'truncated_key'   => substr( $consumer_key, -7 ),
	),
	array( '%d', '%s', '%s', '%s', '%s', '%s' )
);

if ( false === $inserted ) {
	WP_CLI::error( 'Failed to insert API key: ' . $wpdb->last_error );
}

WP_CLI::success( "API key '{$description}' ({$permissions}) set for user {$login} (key_id {$wpdb->insert_id})." );
