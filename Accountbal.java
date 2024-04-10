import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;

public void checkAccountbal(int newAccNo) {
    try (Connection con = Database.getMyConnection();
         PreparedStatement prs = con.prepareStatement("SELECT * FROM customersacct WHERE iacno = ?")) {
        prs.setInt(1, newAccNo);
        ResultSet rs = prs.executeQuery();

        if (rs.next()) {
            setAccountNo(rs.getInt("iacno"));
            setName(rs.getString("vcusname"));
            setBalance(rs.getDouble("mbal"));
        } else {
            // Handle the case where the account number doesn't exist
            System.out.println("Account number not found.");
        }

        rs.close(); // Close the ResultSet after use
    } catch (SQLException e) {
        // Handle SQLException appropriately, e.g., log the error
        System.err.println(e.getMessage());
    }
}
