package abcbankpack.model;

import abcbankpack.database.Database;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

public class Users {
    private String userId;
    private String username;
    private String password;

    public Users() {
    }

    public boolean isLoginOk(String newUsername, String newPassword) throws SQLException {
        boolean loginOk = false;
        try (
            Connection con = Database.getMyConnection();
            Statement stmt = con.createStatement();
            ResultSet rs = stmt.executeQuery("SELECT * FROM AccountUsers WHERE cusername = '" + newUsername + "' AND cpassword = '" + newPassword + "'");
        ) {
            if (rs.next()) {
                loginOk = true;
            }
        } catch (SQLException sqle) {
            System.out.println(sqle.getMessage());
        }
        return loginOk;
    }
}
