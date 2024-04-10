package abcbankpack.model;
import abcbankpack.database.Database;
import java.sql.connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

Public class Users{
	private String UserId;
	private String Username;
	private String password;
	
Public Users()
{
}

Public boolean isLoginOk(String newUsername, String newPassword)
throws SQLException
{
	boolean loginOk = false;
	try(
		connection con = Database.getMyConnection();
		StatementStrut = con.createstatement();
		ResultSet rs = stmt.ExecuteQuery("select* from AccountUsers where cAv = 100");
	)
	{
		rs.next();
		string users = rs.getString("cusername");
		string passw = rs.getString("cpassword");
		
		if(new Username.equalsIgnoreCase(user.trim())&&newPassword.equals(passW.trim()))
		{
			loginOk = true;
		}
	}
	catch (SQLException sqle)
	{
		System.out.println(sqle.getMessage());
	}
	return loginOk;
}	
}
