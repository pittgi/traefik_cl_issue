// Get the incoming request data
$input = file_get_contents('php://input');

// Check if the input matches "BBB"
if (trim($input) === "BBB") {
    echo "Match found!";
} else {
    echo "No match.";
}
?>
