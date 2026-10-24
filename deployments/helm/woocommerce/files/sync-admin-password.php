<?php
/**
 * Make the admin user's password match the Kubernetes Secret, reading it from
 * a mounted file so it never appears in argv or logs. Only writes when the
 * password differs, so existing sessions survive routine upgrades.
 *
 * Run with: wp eval-file sync-admin-password.php
 */

$path = getenv( 'WP_ADMIN_PASSWORD_FILE' );
if ( ! $path || ! is_readable( $path ) ) {
	WP_CLI::error( 'WP_ADMIN_PASSWORD_FILE is not set or not readable.' );
}
$password = rtrim( file_get_contents( $path ), "\r\n" );
if ( '' === $password ) {
	WP_CLI::error( 'Admin password in the Secret is empty.' );
}

$login = getenv( 'WP_ADMIN_USER' );
$user  = get_user_by( 'login', $login );
if ( ! $user ) {
	WP_CLI::error( "Admin user {$login} does not exist." );
}

if ( wp_check_password( $password, $user->user_pass, $user->ID ) ) {
	WP_CLI::success( "Admin password for {$login} already matches the Secret." );
	return;
}

wp_set_password( $password, $user->ID );
WP_CLI::success( "Admin password for {$login} set from the Secret." );
