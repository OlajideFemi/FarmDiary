import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;

public class Deposit {
    private int accountNo;
    private double amountDep;
    private String amountInWords;

    public Deposit() {
    }

    public boolean isDeposit(int newAccountNo, double newAmountDep, String newAmountInWords) {
        boolean isDepositOk = false;
        try (Connection con = Database.getMyConnection();
             PreparedStatement pre = con.prepareStatement("INSERT INTO deposit (acno, amtdep, amtinwords) VALUES (?, ?, ?)")) {
            pre.setInt(1, newAccountNo);
            pre.setDouble(2, newAmountDep);
            pre.setString(3, newAmountInWords);

            int rowsAffected = pre.executeUpdate();
            if (rowsAffected > 0) {
                isDepositOk = true;
            }
        } catch (SQLException e) {
            System.out.println(e.getMessage());
        }
        return isDepositOk;
    }
}
