package abcbankpack.model;

import abcbankpack.database.Database;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;

public class Customers {
    private int accountNo;
    private String name;
    private String surname;
    private String gender;
    private String homeAddress;
    private String phoneNumber;
    private double balance;
    private String officerId;
    private Connection con;

    public Customers() {
    }

    public boolean isRegister(String newName, String newSurname, String newGender, String newHomeAddress, String newPhoneNumber, double newBalance, String newOfficerId) {
        boolean isRegOk = false;
        try (Connection con = Database.getMyConnection();
             PreparedStatement prs = con.prepareStatement("INSERT INTO CustomerAcct (vCusName, vCusSurname, cSex, vAddress, cPhoneNo, mBal, Staffid) VALUES (?,?,?,?,?,?,?)")) {

            prs.setString(1, newName);
            prs.setString(2, newSurname);
            prs.setString(3, newGender);
            prs.setString(4, newHomeAddress);
            prs.setString(5, newPhoneNumber);
            prs.setDouble(6, newBalance);
            prs.setString(7, newOfficerId);

            int rowsAffected = prs.executeUpdate();
            if (rowsAffected > 0) {
                isRegOk = true;
            }
        } catch (SQLException sqle) {
            sqle.printStackTrace();
        }
        return isRegOk;
    }
}
