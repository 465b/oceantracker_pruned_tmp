################
SettleInPolygon
################

**Description:** 

**class_name:** oceantracker.trajectory_modifiers.settle_in_polygon.SettleInPolygon

**File:** oceantracker/trajectory_modifiers/settle_in_polygon.py

**Inheritance:** _BaseTrajectoryModifier> SettleInPolygon


Parameters:
************

	* ``class_name`` :   ``<class 'str'>``   *<optional>*
		Description: Class name as string A.B.C, used to import this class from python path

		- default: ``None``

	* ``polygon``: nested parameter dictionary
		* ``points`` :   ``array`` **<isrequired>**
			- default: ``None``

	* ``probability_of_settlement`` :   ``<class 'float'>``   *<optional>*
		- default: ``0.0``

	* ``settlement_duration`` :   ``<class 'float'>``   *<optional>*
		- default: ``0.0``
		- min: ``0.0``

	* ``user_note`` :   ``<class 'str'>``   *<optional>*
		- default: ``None``

